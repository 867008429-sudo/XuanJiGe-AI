"""Chat checkpoint and metadata cleanup helpers (P3).

Retention is driven by business-side chat activity timestamps. LangGraph's
SQLite checkpoint tables do not carry a reliable created_at column, so we only
delete threads whose latest chat request/log row is older than the threshold.
"""
import logging

import chat_graph
import db


logger = logging.getLogger('xuanjige.chat.cleanup')


def cleanup_stale_chat_threads(retention_days=30, limit=1000, dry_run=False):
    """Delete stale chat checkpoints plus replay/log metadata.

    Returns a small summary dict for CLI output, tests, and startup logs.
    The quota table is intentionally untouched: retention cleanup should not
    restore free incense for an old chart.
    """
    thread_ids = db.list_stale_chat_threads(retention_days, limit)
    summary = {
        'retention_days': int(retention_days),
        'dry_run': bool(dry_run),
        'threads_found': len(thread_ids),
        'threads_cleaned': 0,
        'metadata_rows_deleted': 0,
        'checkpoint_errors': [],
        'metadata_errors': [],
        'thread_ids': thread_ids,
    }
    if dry_run:
        return summary

    for thread_id in thread_ids:
        checkpoint_ok = True
        try:
            chat_graph.delete_chat_thread(thread_id)
        except Exception as exc:  # noqa: BLE001 cleanup must continue per thread
            checkpoint_ok = False
            logger.exception('failed to delete chat checkpoint: thread=%s', thread_id)
            summary['checkpoint_errors'].append({
                'thread_id': thread_id,
                'error': type(exc).__name__,
            })

        try:
            summary['metadata_rows_deleted'] += db.delete_chat_thread_metadata(thread_id)
        except Exception as exc:  # noqa: BLE001 cleanup must continue per thread
            logger.exception('failed to delete chat metadata: thread=%s', thread_id)
            summary['metadata_errors'].append({
                'thread_id': thread_id,
                'error': type(exc).__name__,
            })
            continue

        if checkpoint_ok:
            summary['threads_cleaned'] += 1

    return summary
