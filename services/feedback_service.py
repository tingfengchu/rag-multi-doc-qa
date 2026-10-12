from db import get_conn
from utils.logger import logger


def save_feedback(user_uuid: str, scope: str, question: str,
                  rewritten_question: str, answer: str,
                  citations: list, rating: str, reason: str = None,
                  file_uuid_filter: str = None):
    import json
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO feedback "
                    "(user_uuid, scope, question, rewritten_question, answer, citations, rating, reason, file_uuid_filter) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (user_uuid, scope, question, rewritten_question, answer,
                     json.dumps(citations, ensure_ascii=False), rating, reason, file_uuid_filter)
                )
        logger.info("feedback_saved", extra={"rating": rating, "scope": scope})
        return True
    except Exception as e:
        logger.error("feedback_failed", extra={"err": str(e)})
        return False


def get_bad_cases(limit: int = 100):
    with get_conn() as conn:
        with conn.cursor(dictionary=True) as cur:
            cur.execute(
                "SELECT * FROM feedback WHERE rating = 'down' "
                "ORDER BY created_at DESC LIMIT %s", (limit,)
            )
            return cur.fetchall()