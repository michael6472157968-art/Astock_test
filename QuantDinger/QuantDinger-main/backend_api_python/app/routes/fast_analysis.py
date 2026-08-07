"""
Fast Analysis API Routes

New high-performance analysis endpoints that replace the slow multi-agent system.
"""
from flask import g, jsonify, request
from app.openapi.blueprint import HumanBlueprint as Blueprint

from app.utils.auth import login_required
from app.utils.logger import get_logger
from app.services.fast_analysis_tasks import (
    acquire_inflight,
    build_inflight_key,
    release_inflight,
    start_async_analysis_task,
    try_refund_credits,
)
from app.services.fast_analysis import get_fast_analysis_service
from app.services.analysis_memory import get_analysis_memory
from app.services.billing_service import get_billing_service

logger = get_logger(__name__)

fast_analysis_blp = Blueprint('fast_analysis', __name__)


@fast_analysis_blp.route('/analyze', methods=['POST'])
@login_required
def analyze():
    """
    Fast AI analysis for any symbol.

    Request body:
        market (required): Crypto, USStock, Forex, etc.
        symbol (required): e.g. BTC/USDT, AAPL
        language (optional, default en-US): Response language
        model (optional): LLM model id, e.g. openai/gpt-5.4
        timeframe (optional, default 1D): Analysis timeframe
        async_submit (optional): Submit as background task
    """
    try:
        data = request.get_json() or {}
        
        market = (data.get('market') or '').strip()
        symbol = (data.get('symbol') or '').strip()
        language = data.get('language', 'en-US')
        model = data.get('model')
        timeframe = data.get('timeframe', '1D')
        async_submit = bool(data.get('async_submit', False))
        
        if not market or not symbol:
            return jsonify({
                'code': 0,
                'msg': 'market and symbol are required',
                'data': None
            }), 400
        
        # Get current user's ID to associate analysis with user
        user_id = getattr(g, 'user_id', None)
        if not user_id:
            return jsonify({'code': 0, 'msg': 'Unauthorized', 'data': None}), 401

        inflight_key = build_inflight_key(user_id, market, symbol, timeframe)
        if not acquire_inflight(inflight_key, ttl_sec=90):
            return jsonify({
                'code': 0,
                'msg': 'Analysis already in progress for this symbol/timeframe. Please wait.',
                'data': {'in_progress': True}
            }), 429

        # Billing / credits (best-effort)
        credits_charged = 0
        remaining_credits = None
        billing_consumed = False
        billing = None
        try:
            billing = get_billing_service()
            if billing.is_billing_enabled():
                credits_charged = int(billing.get_feature_cost('ai_analysis') or 0)
                if credits_charged > 0:
                    ok, msg = billing.check_and_consume(
                        user_id=int(user_id),
                        feature='ai_analysis',
                        reference_id=f"fast_analysis_{market}:{symbol}:{timeframe}"
                    )
                    if not ok:
                        # Standardize insufficient credits message
                        if str(msg or "").startswith('insufficient_credits'):
                            # Format: insufficient_credits:<current>:<cost>
                            parts = str(msg).split(':')
                            cur = float(parts[1]) if len(parts) >= 2 else 0.0
                            req = float(parts[2]) if len(parts) >= 3 else float(credits_charged)
                            return jsonify({
                                'code': 0,
                                'msg': 'Insufficient credits',
                                'data': {
                                    'required': req,
                                    'current': cur,
                                    'shortage': max(0.0, req - cur),
                                }
                            }), 400
                        return jsonify({'code': 0, 'msg': f'Failed to deduct credits: {msg}', 'data': None}), 500
                    billing_consumed = True
                    # Query remaining credits after successful consumption
                    try:
                        remaining_credits = float(billing.get_user_credits(int(user_id)))
                    except Exception:
                        remaining_credits = None
        except Exception as e:
            # Billing failure should not crash analysis by default, but should be visible in logs.
            logger.warning(f"Billing check failed (skipped): {e}", exc_info=True)
        
        service = get_fast_analysis_service()

        # Async submit mode: record "processing" immediately and return task id.
        if async_submit:
            memory = get_analysis_memory()
            pending_id = memory.create_pending_task(
                market=market,
                symbol=symbol,
                language=language,
                model=model or "",
                timeframe=timeframe,
                user_id=user_id
            )
            if not pending_id:
                return jsonify({'code': 0, 'msg': 'Failed to create analysis task', 'data': None}), 500

            start_async_analysis_task(
                int(pending_id), market, symbol, language, model, timeframe,
                int(user_id), inflight_key, int(credits_charged or 0),
            )
            # worker owns inflight release
            inflight_key = None

            return jsonify({
                'code': 1,
                'msg': 'submitted',
                'data': {
                    'task_id': int(pending_id),
                    'memory_id': int(pending_id),
                    'status': 'processing',
                    'market': market,
                    'symbol': symbol,
                    'timeframe': timeframe,
                    'credits_charged': credits_charged,
                    'remaining_credits': remaining_credits,
                }
            })

        result = service.analyze(
            market=market,
            symbol=symbol,
            language=language,
            model=model,
            timeframe=timeframe,
            user_id=user_id
        )
        
        if result.get('error'):
            # Best-effort refund if we already charged but analysis failed.
            if billing_consumed and billing and credits_charged > 0:
                try:
                    try_refund_credits(
                        user_id=int(user_id),
                        amount=int(credits_charged),
                        remark=f'Auto refund: fast-analysis failed ({market}:{symbol}:{timeframe})'
                    )
                    remaining_credits = float(billing.get_user_credits(int(user_id)))
                except Exception as re:
                    logger.error(f"Auto refund failed: {re}", exc_info=True)
            return jsonify({
                'code': 0,
                'msg': result['error'],
                'data': result
            }), 500
        
        # memory_id is already set in service.analyze() -> _store_analysis_memory()
        # No need to store again here (would create duplicates)
        
        return jsonify({
            'code': 1,
            'msg': 'success',
            'data': {
                **(result or {}),
                'market': market,
                'symbol': symbol,
                'timeframe': timeframe,
                'credits_charged': credits_charged,
                'remaining_credits': remaining_credits,
            }
        })
        
    except Exception as e:
        # Best-effort refund on unexpected error after charge.
        try:
            if 'billing_consumed' in locals() and billing_consumed and 'billing' in locals() and billing and credits_charged > 0 and 'user_id' in locals() and user_id:
                try_refund_credits(
                    user_id=int(user_id),
                    amount=int(credits_charged),
                    remark=f'Auto refund: fast-analysis exception ({market}:{symbol}:{timeframe})',
                    reference_id=(
                        f'fast-analysis-refund:{int(pending_id)}'
                        if 'pending_id' in locals() and pending_id else ''
                    ),
                )
        except Exception:
            pass
        try:
            if 'pending_id' in locals() and pending_id:
                get_analysis_memory().fail_pending_task(int(pending_id), str(e))
        except Exception:
            pass
        logger.error(f"Fast analysis API failed: {e}", exc_info=True)
        return jsonify({
            'code': 0,
            'msg': str(e),
            'data': None
        }), 500
    finally:
        try:
            if 'inflight_key' in locals() and inflight_key:
                release_inflight(inflight_key)
        except Exception:
            pass


@fast_analysis_blp.route('/history', methods=['GET'])
@login_required
def get_history():
    """
    Get analysis history for a symbol.
    
    GET /api/fast-analysis/history?market=Crypto&symbol=BTC/USDT&days=7&limit=10
    """
    try:
        market = request.args.get('market', '').strip()
        symbol = request.args.get('symbol', '').strip()
        days = int(request.args.get('days', 7))
        limit = min(int(request.args.get('limit', 10)), 50)
        
        if not market or not symbol:
            return jsonify({
                'code': 0,
                'msg': 'market and symbol are required',
                'data': None
            }), 400
        
        memory = get_analysis_memory()
        history = memory.get_recent(market, symbol, days, limit)
        
        return jsonify({
            'code': 1,
            'msg': 'success',
            'data': {
                'items': history,
                'total': len(history)
            }
        })
        
    except Exception as e:
        logger.error(f"Get history failed: {e}")
        return jsonify({
            'code': 0,
            'msg': str(e),
            'data': None
        }), 500


@fast_analysis_blp.route('/history/all', methods=['GET'])
@login_required
def get_all_history():
    """
    Get all analysis history with pagination.
    
    GET /api/fast-analysis/history/all?page=1&pagesize=20
    """
    try:
        page = int(request.args.get('page', 1))
        pagesize = min(int(request.args.get('pagesize', 20)), 50)
        
        # Get current user's ID to filter history
        user_id = getattr(g, 'user_id', None)
        
        memory = get_analysis_memory()
        result = memory.get_all_history(user_id=user_id, page=page, page_size=pagesize)
        
        return jsonify({
            'code': 1,
            'msg': 'success',
            'data': {
                'list': result['items'],
                'total': result['total'],
                'page': result['page'],
                'pagesize': result['page_size']
            }
        })
        
    except Exception as e:
        logger.error(f"Get all history failed: {e}")
        return jsonify({
            'code': 0,
            'msg': str(e),
            'data': None
        }), 500


@fast_analysis_blp.route('/history/<int:memory_id>', methods=['DELETE'])
@login_required
def delete_history(memory_id: int):
    """
    Delete a history record.
    
    DELETE /api/fast-analysis/history/123
    """
    try:
        # Get current user's ID to ensure they can only delete their own records
        user_id = getattr(g, 'user_id', None)
        
        memory = get_analysis_memory()
        success = memory.delete_history(memory_id, user_id=user_id)
        
        if success:
            return jsonify({
                'code': 1,
                'msg': 'Deleted successfully',
                'data': None
            })
        else:
            return jsonify({
                'code': 0,
                'msg': 'Record not found or no permission',
                'data': None
            }), 404
        
    except Exception as e:
        logger.error(f"Delete history failed: {e}")
        return jsonify({
            'code': 0,
            'msg': str(e),
            'data': None
        }), 500


@fast_analysis_blp.route('/feedback', methods=['POST'])
@login_required
def submit_feedback():
    """
    Submit user feedback on an analysis.

    Request body:
        memory_id (required): Analysis history ID
        feedback (required): helpful, not_helpful, accurate, or inaccurate
    """
    try:
        data = request.get_json() or {}
        
        memory_id = int(data.get('memory_id', 0))
        feedback = (data.get('feedback') or '').strip()
        
        if not memory_id or not feedback:
            return jsonify({
                'code': 0,
                'msg': 'memory_id and feedback are required',
                'data': None
            }), 400
        
        valid_feedback = ['helpful', 'not_helpful', 'accurate', 'inaccurate']
        if feedback not in valid_feedback:
            return jsonify({
                'code': 0,
                'msg': f'feedback must be one of: {valid_feedback}',
                'data': None
            }), 400
        
        memory = get_analysis_memory()
        success = memory.record_feedback(memory_id, feedback)
        
        return jsonify({
            'code': 1 if success else 0,
            'msg': 'success' if success else 'failed',
            'data': None
        })
        
    except Exception as e:
        logger.error(f"Submit feedback failed: {e}")
        return jsonify({
            'code': 0,
            'msg': str(e),
            'data': None
        }), 500


@fast_analysis_blp.route('/performance', methods=['GET'])
@login_required
def get_performance():
    """
    Get AI analysis performance statistics.
    
    GET /api/fast-analysis/performance?market=Crypto&symbol=BTC/USDT&days=30
    """
    try:
        market = request.args.get('market', '').strip() or None
        symbol = request.args.get('symbol', '').strip() or None
        days = int(request.args.get('days', 30))
        
        memory = get_analysis_memory()
        stats = memory.get_performance_stats(market, symbol, days)
        
        return jsonify({
            'code': 1,
            'msg': 'success',
            'data': stats
        })
        
    except Exception as e:
        logger.error(f"Get performance failed: {e}")
        return jsonify({
            'code': 0,
            'msg': str(e),
            'data': None
        }), 500


@fast_analysis_blp.route('/similar-patterns', methods=['GET'])
@login_required
def get_similar_patterns():
    """
    Get similar historical patterns for current market conditions.
    
    GET /api/fast-analysis/similar-patterns?market=Crypto&symbol=BTC/USDT
    """
    try:
        market = request.args.get('market', '').strip()
        symbol = request.args.get('symbol', '').strip()
        
        if not market or not symbol:
            return jsonify({
                'code': 0,
                'msg': 'market and symbol are required',
                'data': None
            }), 400
        
        # Get current indicators
        service = get_fast_analysis_service()
        data = service._collect_market_data(market, symbol)
        indicators = data.get('indicators', {})
        
        # Find similar patterns
        memory = get_analysis_memory()
        patterns = memory.get_similar_patterns(market, symbol, indicators)
        
        return jsonify({
            'code': 1,
            'msg': 'success',
            'data': {
                'patterns': patterns,
                'current_indicators': {
                    'rsi': indicators.get('rsi', {}).get('value'),
                    'macd_signal': indicators.get('macd', {}).get('signal'),
                    'trend': indicators.get('moving_averages', {}).get('trend'),
                }
            }
        })
        
    except Exception as e:
        logger.error(f"Get similar patterns failed: {e}")
        return jsonify({
            'code': 0,
            'msg': str(e),
            'data': None
        }), 500

# openapi-compat: legacy import name
fast_analysis_bp = fast_analysis_blp
