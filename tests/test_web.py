from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


TEST_DATA = tempfile.TemporaryDirectory()


def require_disposable_database_path(
    path: str | Path,
    temporary_root: str | Path,
    *,
    must_not_exist: bool,
) -> Path:
    """Reject inherited, existing, or non-temporary database paths in tests."""
    root = Path(temporary_root).resolve(strict=True)
    candidate = Path(path).resolve(strict=False)
    if candidate.parent != root or candidate.name != "messages.db":
        raise RuntimeError("Web tests require their own disposable SQLite database")
    if must_not_exist and candidate.exists():
        raise RuntimeError("Web test SQLite database must not already exist")
    return candidate


TEST_DATABASE_PATH = require_disposable_database_path(
    Path(TEST_DATA.name) / "messages.db",
    TEST_DATA.name,
    must_not_exist=True,
)
os.environ.update(
    {
        "TG_API_ID": "12345",
        "TG_API_HASH": "test-api-hash",
        "TG_PHONE": "+10000000000",
        "TG_SESSION_PATH": f"{TEST_DATA.name}/telegram",
        "PUSH_BOT_TOKEN": "test-bot-token",
        "PUSH_CHAT_ID": "123456",
        "WATCH_CHAT_IDS": "-1001234567890",
        "DATABASE_PATH": str(TEST_DATABASE_PATH),
        "WEB_USERNAME": "admin",
        "WEB_PASSWORD": "a-secure-test-password",
        "WEB_COOKIE_SECURE": "false",
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import (  # noqa: E402
    EXAMPLE_WEB_PASSWORD,
    MIN_WEB_PASSWORD_LENGTH,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    LoginRateLimiter,
    SessionStore,
    is_valid_web_password,
)
from app.database import Database, MessageRecord, to_iso, utc_now  # noqa: E402
from app.scoring import normalize_text  # noqa: E402
from app.web import LOGIN_LIMITER, SESSION_STORE, app  # noqa: E402
from tests.fake_openai import FakeOpenAIServer  # noqa: E402


ORIGIN = "http://testserver"
VALID_USERNAME = "admin"
VALID_PASSWORD = "a-secure-test-password"


def write_headers(*, origin: str = ORIGIN, host: str = "testserver") -> dict[str, str]:
    return {
        "Host": host,
        "Origin": origin,
        "X-Requested-With": "admin-ui",
    }


def audit_record(message_id: int, text: str, *, chat_id: int = -1001234567890) -> MessageRecord:
    now = utc_now()
    return MessageRecord(
        chat_id=chat_id,
        message_id=message_id,
        chat_name="去标识会话",
        chat_username=None,
        sender_id=None,
        sender_name="匿名",
        sent_at=to_iso(now),
        text=text,
        reply_to_message_id=None,
        thread_root_id=message_id,
        base_score=0,
        reasons=(),
        link=None,
        normalized_text=normalize_text(text),
        primary_url=None,
        created_at=to_iso(now),
    )


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fake = FakeOpenAIServer().start()
        database = Database(os.environ["DATABASE_PATH"])
        now = utc_now()
        database.initialize_runtime_config(
            important_keywords=("紧急",),
            trusted_sender_ids=frozenset(),
            watch_chat_ids=frozenset({-1001234567890}),
            immediate_score=80,
            now=now,
        )
        database.replace_available_chats(
            [
                {
                    "chat_id": -1001234567890,
                    "chat_name": "测试群",
                    "chat_type": "group",
                    "username": "test_group",
                }
            ],
            now=now,
        )
        record = MessageRecord(
            chat_id=-1001234567890,
            message_id=7001,
            chat_name="测试群",
            chat_username="test_group",
            sender_id=77,
            sender_name="测试发送者",
            sent_at=to_iso(now),
            text="需要模型分析的测试消息",
            reply_to_message_id=None,
            thread_root_id=7001,
            base_score=25,
            reasons=("重要关键词 +25",),
            link=None,
            normalized_text=normalize_text("需要模型分析的测试消息"),
            primary_url=None,
            created_at=to_iso(now),
        )
        database.insert_message(record)
        cls.message_row_id = database.get_message(-1001234567890, 7001)["id"]
        database.close()
        cls.client_context = TestClient(app, base_url=ORIGIN)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)
        cls.fake.close()
        TEST_DATA.cleanup()

    def setUp(self) -> None:
        SESSION_STORE.clear()
        LOGIN_LIMITER.clear()
        self.client.cookies.clear()
        self.fake.requests.clear()
        self.fake.chat_status = 200
        self.fake.classification_status = 200
        self.fake.scoring_status = 200
        self.fake.dedupe_status = 200
        database = Database(os.environ["DATABASE_PATH"])
        now = utc_now()
        database.recover_pending_analyses(now=now)
        with database.connection:
            database.connection.execute("DELETE FROM analysis_jobs")
            database.connection.execute("DELETE FROM analysis_chat_schedule")
            database.connection.execute("DELETE FROM deliveries")
            database.connection.execute(
                """
                UPDATE messages
                SET ai_status = 'not_analyzed', analysis_queue_requested = 0,
                    analysis_queue_state = NULL, analysis_queue_attempts = 0,
                    analysis_queue_available_at = NULL,
                    analysis_queue_error_category = NULL,
                    analysis_queue_manual = 0, push_eligible = 0,
                    push_gate_reason = 'awaiting_analysis', push_ready_at = NULL,
                    semantic_dedupe_status = 'historical_unreviewed',
                    semantic_dedupe_model = NULL,
                    semantic_dedupe_effort = NULL,
                    semantic_dedupe_confidence = NULL,
                    semantic_dedupe_reason = NULL,
                    semantic_dedupe_response_text = NULL,
                    semantic_dedupe_matched_message_id = NULL,
                    semantic_dedupe_material_update = 0,
                    semantic_dedupe_checked_at = NULL,
                    semantic_dedupe_error_category = NULL,
                    semantic_dedupe_candidate_count = 0,
                    notification_prepare_status = 'historical_unprepared',
                    notification_prepare_model = NULL,
                    notification_prepare_effort = NULL,
                    notification_title = NULL, notification_body = NULL,
                    notification_prepare_response_text = NULL,
                    notification_prepare_checked_at = NULL,
                    notification_prepare_error_category = NULL
                WHERE id = ?
                """,
                (self.message_row_id,),
            )
        database.update_model_config(
            enabled=True,
            base_url=self.fake.base_url,
            model="test-model-a",
            classification_model="test-model-z",
            api_key=self.fake.api_key,
            clear_api_key=False,
            now=now,
            reasoning_effort="medium",
            classification_reasoning_effort="high",
            semantic_dedupe_model="test-dedupe-model",
            semantic_dedupe_reasoning_effort="medium",
            notification_model="test-notification-model",
            notification_reasoning_effort="low",
        )
        database.initialize_push_config(
            telegram_bot_token="test-bot-token",
            telegram_chat_id="123456",
            now=now,
        )
        database.update_push_config(
            telegram_enabled=True,
            telegram_bot_token="test-bot-token",
            clear_telegram_bot_token=False,
            telegram_chat_id="123456",
            ntfy_enabled=False,
            ntfy_base_url="https://ntfy.example.com",
            ntfy_topic="",
            ntfy_access_token="",
            clear_ntfy_access_token=True,
            now=now,
        )
        database.close()

    def login(
        self,
        *,
        username: str = VALID_USERNAME,
        password: str = VALID_PASSWORD,
        headers: dict[str, str] | None = None,
    ):
        return self.client.post(
            "/api/auth/login",
            headers=headers or write_headers(),
            json={"username": username, "password": password},
        )

    def test_public_routes_and_protected_api(self) -> None:
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "no-store")
        denied = self.client.get("/api/stats")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json(), {"detail": "需要登录"})
        self.assertNotIn("www-authenticate", denied.headers)

    def test_database_path_is_forced_to_a_fresh_disposable_directory(self) -> None:
        configured = require_disposable_database_path(
            os.environ["DATABASE_PATH"],
            TEST_DATA.name,
            must_not_exist=False,
        )
        self.assertEqual(configured, TEST_DATABASE_PATH)
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "messages.db"
            existing.touch()
            with self.assertRaises(RuntimeError):
                require_disposable_database_path(
                    existing,
                    directory,
                    must_not_exist=True,
                )
            with self.assertRaises(RuntimeError):
                require_disposable_database_path(
                    "/database/messages.db",
                    directory,
                    must_not_exist=False,
                )

    def test_web_password_minimum_policy(self) -> None:
        self.assertEqual(MIN_WEB_PASSWORD_LENGTH, 8)
        self.assertFalse(is_valid_web_password("x" * 7))
        self.assertTrue(is_valid_web_password("x" * 8))
        self.assertFalse(is_valid_web_password(EXAMPLE_WEB_PASSWORD))

    def test_basic_auth_is_no_longer_accepted(self) -> None:
        encoded = base64.b64encode(
            f"{VALID_USERNAME}:{VALID_PASSWORD}".encode("utf-8")
        ).decode("ascii")
        response = self.client.get(
            "/api/stats",
            headers={"Authorization": f"Basic {encoded}"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("www-authenticate", response.headers)

    def test_session_status_and_valid_login(self) -> None:
        before = self.client.get("/api/auth/session")
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.json(), {"authenticated": False, "username": None})

        logged_in = self.login()
        self.assertEqual(logged_in.status_code, 200)
        self.assertEqual(
            logged_in.json(),
            {"authenticated": True, "username": VALID_USERNAME},
        )
        session = self.client.get("/api/auth/session")
        self.assertEqual(session.status_code, 200)
        self.assertTrue(session.json()["authenticated"])
        self.assertEqual(session.json()["username"], VALID_USERNAME)

    def test_login_cookie_attributes_and_no_token_in_body(self) -> None:
        response = self.login()
        self.assertEqual(response.status_code, 200)
        cookie_header = response.headers["set-cookie"].casefold()
        self.assertIn("httponly", cookie_header)
        self.assertIn("samesite=strict", cookie_header)
        self.assertIn("path=/", cookie_header)
        self.assertIn(f"max-age={SESSION_TTL_SECONDS}", cookie_header)
        self.assertNotIn("domain=", cookie_header)
        self.assertNotIn("; secure", cookie_header)
        token = self.client.cookies.get(SESSION_COOKIE_NAME)
        self.assertTrue(token)
        self.assertNotIn(token, json.dumps(response.json()))

    def test_invalid_login_is_generic_and_both_digests_are_compared(self) -> None:
        with patch("app.web.secrets.compare_digest", wraps=__import__("secrets").compare_digest) as compared:
            wrong_user = self.login(username="not-the-admin")
        self.assertEqual(compared.call_count, 2)
        wrong_password = self.login(password="not-the-password")
        self.assertEqual(wrong_user.status_code, 401)
        self.assertEqual(wrong_password.status_code, 401)
        self.assertEqual(wrong_user.json(), wrong_password.json())
        self.assertEqual(wrong_user.json()["detail"], "用户名或密码不正确")

    def test_login_field_limits_do_not_echo_input(self) -> None:
        response = self.client.post(
            "/api/auth/login",
            headers=write_headers(),
            json={"username": "x" * 129, "password": "y" * 1_025},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": "请求字段格式无效"})

    def test_login_requires_json_custom_header_and_same_origin(self) -> None:
        missing_header = self.client.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN},
            json={"username": VALID_USERNAME, "password": VALID_PASSWORD},
        )
        self.assertEqual(missing_header.status_code, 403)

        missing_origin = self.client.post(
            "/api/auth/login",
            headers={"X-Requested-With": "admin-ui"},
            json={"username": VALID_USERNAME, "password": VALID_PASSWORD},
        )
        self.assertEqual(missing_origin.status_code, 403)

        cross_origin = self.login(headers=write_headers(origin="http://attacker.invalid"))
        self.assertEqual(cross_origin.status_code, 403)
        host_mismatch = self.login(headers=write_headers(host="other.invalid"))
        self.assertEqual(host_mismatch.status_code, 403)

        simple_form = self.client.post(
            "/api/auth/login",
            headers={
                **write_headers(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            content="username=admin&password=hidden",
        )
        self.assertEqual(simple_form.status_code, 415)

        referer_only = self.login(
            headers={
                "Host": "testserver",
                "Referer": f"{ORIGIN}/",
                "X-Requested-With": "admin-ui",
            }
        )
        self.assertEqual(referer_only.status_code, 200)

    def test_per_ip_login_limit_and_success_reset(self) -> None:
        for _ in range(4):
            self.assertEqual(self.login(password="wrong-before-success").status_code, 401)
        self.assertEqual(self.login().status_code, 200)
        self.client.cookies.clear()
        for _ in range(5):
            self.assertEqual(self.login(password="wrong-after-success").status_code, 401)
        limited = self.login(password="wrong-after-success")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["detail"], "登录暂不可用，请稍后再试")

    def test_rate_limiter_is_global_bounded_and_expires(self) -> None:
        clock = [100.0]
        limiter = LoginRateLimiter(
            window_seconds=10,
            per_ip_limit=2,
            global_limit=3,
            max_tracked_ips=2,
            clock=lambda: clock[0],
        )
        limiter.record_failure("192.0.2.1")
        limiter.record_failure("192.0.2.2")
        limiter.record_failure("192.0.2.3")
        self.assertEqual(limiter.tracked_counts(), (2, 3))
        self.assertFalse(limiter.allowed("192.0.2.99"))
        clock[0] = 111.0
        self.assertEqual(limiter.tracked_counts(), (0, 0))
        self.assertTrue(limiter.allowed("192.0.2.99"))

    def test_session_store_is_bounded_and_expires(self) -> None:
        clock = [200.0]
        store = SessionStore(ttl_seconds=10, max_sessions=2, clock=lambda: clock[0])
        first, _ = store.create("admin")
        second, _ = store.create("admin")
        third, _ = store.create("admin")
        self.assertIsNone(store.validate(first))
        self.assertIsNotNone(store.validate(second))
        self.assertIsNotNone(store.validate(third))
        self.assertEqual(len(store), 2)
        clock[0] = 211.0
        self.assertIsNone(store.validate(second))
        self.assertEqual(len(store), 0)

    def test_logout_revokes_server_session_and_old_cookie_replay(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        old_token = self.client.cookies.get(SESSION_COOKIE_NAME)
        logout = self.client.post(
            "/api/auth/logout",
            headers=write_headers(),
            json={},
        )
        self.assertEqual(logout.status_code, 200)
        self.assertFalse(logout.json()["authenticated"])
        deletion = logout.headers["set-cookie"].casefold()
        self.assertIn("max-age=0", deletion)
        self.assertIn("httponly", deletion)
        self.assertIn("samesite=strict", deletion)
        replay = self.client.get(
            "/api/stats",
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={old_token}"},
        )
        self.assertEqual(replay.status_code, 401)

    def test_expired_cookie_cannot_access_protected_api(self) -> None:
        clock = [300.0]
        original_clock = SESSION_STORE._clock
        try:
            SESSION_STORE._clock = lambda: clock[0]
            token, _ = SESSION_STORE.create(VALID_USERNAME)
            allowed = self.client.get(
                "/api/stats",
                headers={"Cookie": f"{SESSION_COOKIE_NAME}={token}"},
            )
            self.assertEqual(allowed.status_code, 200)
            clock[0] += SESSION_TTL_SECONDS + 1
            expired = self.client.get(
                "/api/stats",
                headers={"Cookie": f"{SESSION_COOKIE_NAME}={token}"},
            )
            self.assertEqual(expired.status_code, 401)
        finally:
            SESSION_STORE._clock = original_clock
            SESSION_STORE.clear()

    def test_protected_write_requires_session_csrf_origin_and_json(self) -> None:
        payload = {
            "important_keywords": ["紧急", "恢复"],
            "trusted_sender_ids": [123, 456],
            "watch_chat_ids": [-1001234567890],
            "immediate_score": 60,
        }
        unauthenticated = self.client.put(
            "/api/config",
            headers=write_headers(),
            json=payload,
        )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(self.login().status_code, 200)

        no_csrf = self.client.put(
            "/api/config",
            headers={"Origin": ORIGIN},
            json=payload,
        )
        self.assertEqual(no_csrf.status_code, 403)
        no_origin = self.client.put(
            "/api/config",
            headers={"X-Requested-With": "admin-ui"},
            json=payload,
        )
        self.assertEqual(no_origin.status_code, 403)
        wrong_type = self.client.put(
            "/api/config",
            headers={**write_headers(), "Content-Type": "text/plain"},
            content=json.dumps(payload),
        )
        self.assertEqual(wrong_type.status_code, 415)
        accepted = self.client.put(
            "/api/config",
            headers=write_headers(),
            json=payload,
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["immediate_score"], 60)
        invalid_threshold = self.client.put(
            "/api/config",
            headers=write_headers(),
            json={**payload, "immediate_score": 80},
        )
        self.assertEqual(invalid_threshold.status_code, 422)

    def test_message_query_is_available_after_login(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        response = self.client.get("/api/messages")
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", response.json())

    def test_feedback_records_are_protected_and_do_not_expose_internal_ids(self) -> None:
        self.assertEqual(self.client.get("/api/feedback").status_code, 401)
        database = Database(os.environ["DATABASE_PATH"])
        now = utc_now()
        with database.connection:
            database.connection.execute(
                """
                INSERT OR REPLACE INTO feedback_records(
                    public_id, message_row_id, vote, voted_at, event_count,
                    title, source_name, content_kind, source_key,
                    interest_tags_json, ai_score, created_at, updated_at
                ) VALUES(?, ?, 'up', ?, 1, ?, ?, 'news', ?, '[]', 82, ?, ?)
                """,
                (
                    "web-feedback-internal-id",
                    self.message_row_id,
                    to_iso(now),
                    "去标识反馈标题",
                    "去标识来源",
                    "internal-source-key",
                    to_iso(now),
                    to_iso(now),
                ),
            )
        database.close()
        try:
            self.assertEqual(self.login().status_code, 200)
            response = self.client.get("/api/feedback?hours=2160")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertGreaterEqual(payload["summary"]["total"], 1)
            item = next(
                value
                for value in payload["items"]
                if value["title"] == "去标识反馈标题"
            )
            self.assertEqual(item["vote"], "up")
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("web-feedback-internal-id", serialized)
            self.assertNotIn("internal-source-key", serialized)
            self.assertNotIn("public_id", serialized)
        finally:
            database = Database(os.environ["DATABASE_PATH"], initialize=False)
            with database.connection:
                database.connection.execute(
                    "DELETE FROM feedback_records WHERE public_id = ?",
                    ("web-feedback-internal-id",),
                )
            database.close()

    def test_semantic_dedupe_raw_response_is_detail_only(self) -> None:
        marker = '{"same_event":true,"reason":"detail-only"}'
        notification_marker = '{"title":"detail-only","body":"detail-only"}'
        database = Database(os.environ["DATABASE_PATH"])
        with database.connection:
            database.connection.execute(
                """
                UPDATE messages
                SET semantic_dedupe_status = 'suppressed',
                    semantic_dedupe_model = 'test-model-z',
                    semantic_dedupe_confidence = 98,
                    semantic_dedupe_reason = '同一事件没有实质更新',
                    semantic_dedupe_response_text = ?,
                    notification_prepare_status = 'success',
                    notification_prepare_response_text = ?,
                    semantic_dedupe_matched_message_id = 1,
                    semantic_dedupe_candidate_count = 2
                WHERE id = ?
                """,
                (marker, notification_marker, self.message_row_id),
            )
        database.close()
        self.assertEqual(self.login().status_code, 200)
        listed = self.client.get(
            "/api/messages?hours=72&similar_status=all"
        ).json()["items"]
        row = next(item for item in listed if item["id"] == self.message_row_id)
        self.assertNotIn("semantic_dedupe_response_text", row)
        self.assertNotIn("notification_prepare_response_text", row)
        detail = self.client.get(f"/api/messages/{self.message_row_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["semantic_dedupe_response_text"], marker)
        self.assertEqual(
            detail.json()["notification_prepare_response_text"], notification_marker
        )

    def test_message_query_can_select_push_eligible_news(self) -> None:
        database = Database(os.environ["DATABASE_PATH"])
        now = utc_now()
        message_id = 7988
        database.insert_message(
            MessageRecord(
                chat_id=-1001234567890,
                message_id=message_id,
                chat_name="测试群",
                chat_username="test_group",
                sender_id=88,
                sender_name="测试发送者",
                sent_at=to_iso(now),
                text="已通过资讯评分的测试消息",
                reply_to_message_id=None,
                thread_root_id=message_id,
                base_score=0,
                reasons=(),
                link=None,
                normalized_text=normalize_text("已通过资讯评分的测试消息"),
                primary_url=None,
                created_at=to_iso(now),
            )
        )
        with database.connection:
            database.connection.execute(
                """
                UPDATE messages
                SET push_eligible = 1, ai_status = 'success',
                    ai_category = 'external_information', ai_score = 72, score = 72
                WHERE chat_id = ? AND message_id = ?
                """,
                (-1001234567890, message_id),
            )
        eligible_id = database.get_message(-1001234567890, message_id)["id"]
        database.close()

        self.assertEqual(self.login().status_code, 200)
        response = self.client.get("/api/messages?push_status=eligible")
        self.assertEqual(response.status_code, 200)
        self.assertIn(eligible_id, {row["id"] for row in response.json()["items"]})
        self.assertTrue(all(row["push_eligible"] for row in response.json()["items"]))
        self.assertEqual(
            self.client.get("/api/messages?push_status=unsupported").status_code,
            422,
        )

    def test_attention_query_only_returns_successful_external_news(self) -> None:
        database = Database(os.environ["DATABASE_PATH"])
        cases = (
            (8101, "attention-audit 可推送资讯", "passed", "success", "external_information", 72, 1),
            (8102, "attention-audit 高相关新闻", "passed", "success", "external_information", 91, 0),
            (8103, "attention-audit 排队消息", "passed", "queued", "external_information", 99, 0),
            (8104, "attention-audit 前置过滤", "filtered", "success", "external_information", 99, 1),
            (8105, "attention-audit 群内治理", "passed", "success", "internal_governance", 99, 1),
            (8106, "attention-audit 推广广告", "passed", "success", "promotion_spam", 99, 1),
            (8107, "attention-audit 一般资讯", "passed", "success", "external_information", 45, 0),
        )
        expected: set[int] = set()
        for message_id, text, prefilter, status, category, score, eligible in cases:
            database.insert_message(audit_record(message_id, text))
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET prefilter_status = ?, ai_status = ?, ai_category = ?,
                        ai_score = ?, score = ?, push_eligible = ?
                    WHERE chat_id = ? AND message_id = ?
                    """,
                    (
                        prefilter,
                        status,
                        category,
                        score,
                        score,
                        eligible,
                        -1001234567890,
                        message_id,
                    ),
                )
            row_id = int(database.get_message(-1001234567890, message_id)["id"])
            if message_id == 8101:
                expected.add(row_id)
        database.close()

        self.assertEqual(self.login().status_code, 200)
        response = self.client.get(
            "/api/messages?attention_only=true&prefilter_status=all&q=attention-audit"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual({int(row["id"]) for row in response.json()["items"]}, expected)
        self.assertNotIn(
            8102,
            {int(row["message_id"]) for row in response.json()["items"]},
        )

    def test_min_score_filters_strictly_by_successful_ai_score(self) -> None:
        database = Database(os.environ["DATABASE_PATH"])
        cases = (
            (8121, 95, 0, "not_analyzed"),
            (8122, 95, 0, "success"),
            (8123, 5, 19, "success"),
            (8124, 5, 20, "success"),
            (8125, 5, 60, "success"),
        )
        expected_20: set[int] = set()
        expected_60: set[int] = set()
        for message_id, local_score, ai_score, ai_status in cases:
            database.insert_message(audit_record(message_id, f"score-filter-{message_id}"))
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET base_score = ?, score = ?, ai_score = ?, ai_status = ?,
                        prefilter_status = 'passed'
                    WHERE chat_id = ? AND message_id = ?
                    """,
                    (
                        local_score,
                        local_score,
                        ai_score,
                        ai_status,
                        -1001234567890,
                        message_id,
                    ),
                )
            row_id = int(database.get_message(-1001234567890, message_id)["id"])
            if ai_status == "success" and ai_score >= 20:
                expected_20.add(row_id)
            if ai_status == "success" and ai_score >= 60:
                expected_60.add(row_id)
        database.close()

        self.assertEqual(self.login().status_code, 200)
        response_20 = self.client.get("/api/messages?q=score-filter-&min_score=20")
        self.assertEqual(response_20.status_code, 200)
        self.assertEqual(
            {int(row["id"]) for row in response_20.json()["items"]},
            expected_20,
        )
        self.assertTrue(
            all(int(row["ai_score"]) >= 20 for row in response_20.json()["items"])
        )

        response_60 = self.client.get("/api/messages?q=score-filter-&min_score=60")
        self.assertEqual(response_60.status_code, 200)
        self.assertEqual(
            {int(row["id"]) for row in response_60.json()["items"]},
            expected_60,
        )

    def test_status_reports_queue_retry_and_failure_category(self) -> None:
        database = Database(os.environ["DATABASE_PATH"])
        now = utc_now()
        database.insert_message(
            audit_record(8110, "queue-audit 外部安全更新"),
            enqueue_analysis=True,
            now=now,
        )
        claimed = database.claim_next_analysis_job(now=now)
        self.assertIsNotNone(claimed)
        database.retry_analysis_job(
            int(claimed["id"]),
            now=now,
            delay_seconds=30,
            error_category="network_error",
            error_stage="classification",
        )
        database.record_queue_metric_sample(now=now)
        database.close()

        self.assertEqual(self.login().status_code, 200)
        response = self.client.get("/api/status?hours=24")
        self.assertEqual(response.status_code, 200)
        analysis = response.json()["analysis_queue"]
        self.assertEqual(analysis["retry"], 1)
        self.assertEqual(analysis["health"], "degraded")
        self.assertIn(
            "network_error",
            {item["category"] for item in analysis["error_categories"]},
        )
        payload = response.json()
        self.assertEqual(len(payload["queue_history"]), 1)
        self.assertEqual(payload["queue_history"][0]["analysis_retry"], 1)
        self.assertEqual(payload["queue_history_meta"]["sample_interval_seconds"], 10)
        self.assertEqual(payload["queue_history_meta"]["retention_days"], 3)

    def test_get_routes_do_not_run_migrations_or_runtime_initialization(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        with patch.object(
            Database,
            "_initialize_schema",
            side_effect=AssertionError("GET must not migrate"),
        ), patch.object(
            Database,
            "initialize_runtime_config",
            side_effect=AssertionError("GET must not initialize config"),
        ):
            self.assertEqual(self.client.get("/api/config").status_code, 200)
            self.assertEqual(self.client.get("/api/messages").status_code, 200)
            self.assertEqual(self.client.get("/api/status").status_code, 200)
            self.assertEqual(self.client.get("/api/sources").status_code, 200)
            self.assertEqual(self.client.get("/api/feedback").status_code, 200)

    def test_recorded_chat_options_exclude_empty_chats(self) -> None:
        database = Database(os.environ["DATABASE_PATH"])
        database.replace_available_chats(
            [
                {
                    "chat_id": -1001234567890,
                    "chat_name": "已有记录会话",
                    "chat_type": "group",
                    "username": None,
                },
                {
                    "chat_id": -1002222222222,
                    "chat_name": "空会话",
                    "chat_type": "channel",
                    "username": None,
                },
            ],
            now=utc_now(),
        )
        database.close()
        self.assertEqual(self.login().status_code, 200)
        response = self.client.get("/api/chats?hours=720&recorded_only=true")
        self.assertEqual(response.status_code, 200)
        chat_ids = {int(row["chat_id"]) for row in response.json()["items"]}
        self.assertIn(-1001234567890, chat_ids)
        self.assertNotIn(-1002222222222, chat_ids)

    def test_information_source_api_is_protected_validated_and_round_trips(self) -> None:
        self.assertEqual(self.client.get("/api/sources").status_code, 401)
        self.assertEqual(self.login().status_code, 200)
        database = Database(os.environ["DATABASE_PATH"])
        database.connection.execute("DELETE FROM information_source_items")
        database.connection.execute("DELETE FROM information_sources")
        database.connection.commit()
        database.close()
        created = self.client.post(
            "/api/sources",
            headers=write_headers(),
            json={
                "kind": "rss",
                "name": "厂商安全公告",
                "url": "https://feeds.example.com/security.xml",
                "enabled": True,
                "poll_interval_minutes": 15,
            },
        )
        self.assertEqual(created.status_code, 200)
        item = created.json()["item"]
        self.assertEqual(item["poll_state"], "idle")
        self.assertFalse(item["initialized"])
        listed = self.client.get("/api/sources")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["items"]), 1)

        updated = self.client.put(
            f"/api/sources/{item['id']}",
            headers=write_headers(),
            json={
                "kind": "rss",
                "name": "厂商正式公告",
                "url": "https://feeds.example.com/releases.atom",
                "enabled": True,
                "poll_interval_minutes": 30,
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["item"]["generation"], 2)
        refreshed = self.client.post(
            f"/api/sources/{item['id']}/refresh",
            headers=write_headers(),
            json={},
        )
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.json()["item"]["poll_state"], "idle")

        unsafe = self.client.post(
            "/api/sources",
            headers=write_headers(),
            json={
                "kind": "rss",
                "name": "内网地址",
                "url": "https://127.0.0.1/feed",
                "enabled": True,
                "poll_interval_minutes": 15,
            },
        )
        self.assertEqual(unsafe.status_code, 422)

        github = self.client.post(
            "/api/sources",
            headers=write_headers(),
            json={
                "kind": "github_releases",
                "name": "Example releases",
                "url": "example/tool",
                "enabled": True,
                "poll_interval_minutes": 30,
                "include_prereleases": True,
            },
        )
        self.assertEqual(github.status_code, 200)
        self.assertEqual(github.json()["item"]["settings"]["repository"], "example/tool")
        self.assertTrue(github.json()["item"]["settings"]["include_prereleases"])

        advisory = self.client.post(
            "/api/sources",
            headers=write_headers(),
            json={
                "kind": "github_advisories",
                "name": "npm 高危公告",
                "url": "",
                "enabled": True,
                "poll_interval_minutes": 60,
                "ecosystem": "npm",
                "minimum_severity": "high",
                "keywords": ["react", "node"],
            },
        )
        self.assertEqual(advisory.status_code, 200)
        self.assertEqual(advisory.json()["item"]["settings"]["ecosystem"], "npm")
        self.assertEqual(
            advisory.json()["item"]["settings"]["minimum_severity"], "high"
        )
        self.assertNotIn("test-only-token", advisory.text)

        kev = self.client.post(
            "/api/sources",
            headers=write_headers(),
            json={
                "kind": "cisa_kev",
                "name": "CISA KEV",
                "url": "",
                "enabled": True,
                "poll_interval_minutes": 60,
            },
        )
        self.assertEqual(kev.status_code, 200)
        self.assertEqual(kev.json()["item"]["kind"], "cisa_kev")
        self.assertEqual(
            kev.json()["item"]["url"],
            "https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
        )

        nvd = self.client.post(
            "/api/sources",
            headers=write_headers(),
            json={
                "kind": "nvd_cve",
                "name": "NVD 重点产品",
                "url": "",
                "enabled": True,
                "poll_interval_minutes": 60,
                "keywords": ["nginx", "openssl"],
            },
        )
        self.assertEqual(nvd.status_code, 200)
        self.assertEqual(nvd.json()["item"]["settings"]["keywords"], ["nginx", "openssl"])
        status_page = self.client.post(
            "/api/sources",
            headers=write_headers(),
            json={
                "kind": "vendor_status",
                "name": "Example Status",
                "url": "https://status.example.com/",
                "enabled": True,
                "poll_interval_minutes": 15,
            },
        )
        self.assertEqual(status_page.status_code, 200)
        self.assertEqual(status_page.json()["item"]["url"], "https://status.example.com/")
        hacker_news = self.client.post(
            "/api/sources",
            headers=write_headers(),
            json={
                "kind": "hacker_news",
                "name": "HN 技术热点",
                "url": "",
                "enabled": True,
                "poll_interval_minutes": 15,
                "story_list": "best",
                "minimum_score": 120,
                "keywords": ["AI", "security"],
            },
        )
        self.assertEqual(hacker_news.status_code, 200)
        self.assertEqual(hacker_news.json()["item"]["settings"]["minimum_score"], 120)
        self.assertEqual(hacker_news.json()["item"]["settings"]["keywords"], ["ai", "security"])
        bluesky = self.client.post(
            "/api/sources",
            headers=write_headers(),
            json={
                "kind": "bluesky",
                "name": "可信开发者",
                "url": "",
                "enabled": True,
                "poll_interval_minutes": 5,
                "bluesky_handle": "Example.Bsky.Social",
            },
        )
        self.assertEqual(bluesky.status_code, 200)
        self.assertEqual(bluesky.json()["item"]["settings"]["handle"], "example.bsky.social")
        self.assertEqual(
            bluesky.json()["item"]["url"], "https://bsky.app/profile/example.bsky.social"
        )
        mastodon = self.client.post(
            "/api/sources",
            headers=write_headers(),
            json={
                "kind": "mastodon",
                "name": "可信实例账号",
                "url": "",
                "enabled": True,
                "poll_interval_minutes": 15,
                "mastodon_instance_url": "https://social.example",
                "mastodon_timeline_type": "account",
                "mastodon_target": "trusted",
                "source_secret": "test-only-mastodon-token",
            },
        )
        self.assertEqual(mastodon.status_code, 200)
        self.assertTrue(mastodon.json()["item"]["secret_configured"])
        self.assertNotIn("test-only-mastodon-token", mastodon.text)
        mastodon_id = mastodon.json()["item"]["id"]
        listed_after_secret = self.client.get("/api/sources")
        self.assertNotIn("test-only-mastodon-token", listed_after_secret.text)
        saved_mastodon = next(
            row for row in listed_after_secret.json()["items"] if row["id"] == mastodon_id
        )
        self.assertTrue(saved_mastodon["secret_configured"])
        moved = self.client.put(
            f"/api/sources/{mastodon_id}",
            headers=write_headers(),
            json={
                "kind": "mastodon",
                "name": "可信实例账号",
                "url": "",
                "enabled": True,
                "poll_interval_minutes": 15,
                "mastodon_instance_url": "https://other-social.example",
                "mastodon_timeline_type": "account",
                "mastodon_target": "trusted",
            },
        )
        self.assertEqual(moved.status_code, 200)
        self.assertFalse(moved.json()["item"]["secret_configured"])
        missing_mail_password = self.client.post(
            "/api/sources",
            headers=write_headers(),
            json={
                "kind": "newsletter_imap",
                "name": "技术简报",
                "url": "",
                "enabled": True,
                "poll_interval_minutes": 15,
                "imap_host": "imap.vendor.example",
                "imap_port": 993,
                "imap_username": "reader@example.com",
                "imap_mailbox": "INBOX",
            },
        )
        self.assertEqual(missing_mail_password.status_code, 400)
        newsletter = self.client.post(
            "/api/sources",
            headers=write_headers(),
            json={
                "kind": "newsletter_imap",
                "name": "技术简报",
                "url": "",
                "enabled": True,
                "poll_interval_minutes": 15,
                "imap_host": "imap.vendor.example",
                "imap_port": 993,
                "imap_username": "reader@example.com",
                "imap_mailbox": "INBOX",
                "sender_allowlist": ["vendor.example"],
                "source_secret": "test-only-mail-password",
            },
        )
        self.assertEqual(newsletter.status_code, 200)
        self.assertTrue(newsletter.json()["item"]["secret_configured"])
        self.assertNotIn("test-only-mail-password", newsletter.text)
        newsletter_id = newsletter.json()["item"]["id"]
        retained = self.client.put(
            f"/api/sources/{newsletter_id}",
            headers=write_headers(),
            json={
                "kind": "newsletter_imap",
                "name": "技术简报",
                "url": "",
                "enabled": True,
                "poll_interval_minutes": 30,
                "imap_host": "imap.vendor.example",
                "imap_port": 993,
                "imap_username": "reader@example.com",
                "imap_mailbox": "INBOX",
                "sender_allowlist": ["vendor.example"],
            },
        )
        self.assertEqual(retained.status_code, 200)
        self.assertTrue(retained.json()["item"]["secret_configured"])
        unsafe_move = self.client.put(
            f"/api/sources/{newsletter_id}",
            headers=write_headers(),
            json={
                "kind": "newsletter_imap",
                "name": "技术简报",
                "url": "",
                "enabled": True,
                "poll_interval_minutes": 30,
                "imap_host": "imap.other.example",
                "imap_port": 993,
                "imap_username": "reader@example.com",
                "imap_mailbox": "INBOX",
            },
        )
        self.assertEqual(unsafe_move.status_code, 400)
        nvd_key = self.client.put(
            "/api/source-provider-config",
            headers=write_headers(),
            json={"nvd_api_key": "test-only-nvd-key", "clear_nvd_api_key": False},
        )
        self.assertEqual(nvd_key.status_code, 200)
        self.assertTrue(nvd_key.json()["nvd_api_key_configured"])
        self.assertNotIn("test-only-nvd-key", nvd_key.text)

        credential = self.client.put(
            "/api/source-provider-config",
            headers=write_headers(),
            json={"github_token": "test-only-token", "clear_github_token": False},
        )
        self.assertEqual(credential.status_code, 200)
        self.assertTrue(credential.json()["github_token_configured"])
        provider = self.client.get("/api/source-provider-config")
        self.assertEqual(provider.status_code, 200)
        self.assertTrue(provider.json()["github_token_configured"])
        self.assertNotIn("test-only-token", provider.text)
        self.assertNotIn("github_token\"", provider.text)

        chats = self.client.get(
            "/api/chats?hours=720&include_sources=true"
        ).json()["items"]
        source_chat = next(row for row in chats if row["chat_name"] == "厂商正式公告")
        self.assertEqual(source_chat["chat_name"], "厂商正式公告")

    def test_message_query_hides_prefiltered_by_default_and_can_filter_them(self) -> None:
        database = Database(os.environ["DATABASE_PATH"])
        now = utc_now()
        message_id = 7999
        database.insert_message(
            MessageRecord(
                chat_id=-1001234567890,
                message_id=message_id,
                chat_name="测试群",
                chat_username="test_group",
                sender_id=88,
                sender_name="测试发送者",
                sent_at=to_iso(now),
                text="测试前置过滤记录",
                reply_to_message_id=None,
                thread_root_id=message_id,
                base_score=0,
                reasons=(),
                link=None,
                normalized_text=normalize_text("测试前置过滤记录"),
                primary_url=None,
                created_at=to_iso(now),
            )
        )
        with database.connection:
            database.connection.execute(
                """
                UPDATE messages
                SET prefilter_status = 'filtered', ai_status = 'prefiltered',
                    prefilter_reason_code = 'recent_exact_duplicate',
                    prefilter_reason = '同群 72 小时内已处理过相同内容'
                WHERE chat_id = ? AND message_id = ?
                """,
                (-1001234567890, message_id),
            )
        filtered_id = database.get_message(-1001234567890, message_id)["id"]
        database.close()

        self.assertEqual(self.login().status_code, 200)
        default_items = self.client.get("/api/messages").json()["items"]
        self.assertNotIn(filtered_id, {row["id"] for row in default_items})

        all_response = self.client.get("/api/messages?prefilter_status=all")
        self.assertEqual(all_response.status_code, 200)
        all_rows = {row["id"]: row for row in all_response.json()["items"]}
        self.assertIn(filtered_id, all_rows)
        self.assertEqual(
            all_rows[filtered_id]["prefilter_reason_code"],
            "recent_exact_duplicate",
        )

        filtered_response = self.client.get("/api/messages?prefilter_status=filtered")
        self.assertEqual(filtered_response.status_code, 200)
        self.assertIn(filtered_id, {row["id"] for row in filtered_response.json()["items"]})
        self.assertTrue(
            all(row["prefilter_status"] == "filtered" for row in filtered_response.json()["items"])
        )

        invalid = self.client.get("/api/messages?prefilter_status=invalid")
        self.assertEqual(invalid.status_code, 422)

    def test_message_query_groups_semantic_duplicates_under_representative(self) -> None:
        database = Database(os.environ["DATABASE_PATH"])
        database.insert_message(audit_record(8291, "cluster-audit 代表资讯"))
        database.insert_message(
            audit_record(8292, "cluster-audit 同一事件转述", chat_id=-1001234567890)
        )
        representative = database.get_message(-1001234567890, 8291)
        duplicate = database.get_message(-1001234567890, 8292)
        with database.connection:
            database.connection.execute(
                """
                UPDATE messages
                SET prefilter_status = 'passed', ai_status = 'success',
                    ai_category = 'external_information', content_kind = 'news',
                    ai_score = 82, score = 82, push_eligible = 1,
                    semantic_dedupe_status = 'unique'
                WHERE id = ?
                """,
                (representative["id"],),
            )
            database.connection.execute(
                """
                UPDATE messages
                SET prefilter_status = 'passed', ai_status = 'success',
                    ai_category = 'external_information', content_kind = 'news',
                    ai_score = 81, score = 81, push_eligible = 0,
                    semantic_dedupe_status = 'suppressed',
                    semantic_dedupe_matched_message_id = ?,
                    semantic_dedupe_reason = '同一事件的跨来源转述'
                WHERE id = ?
                """,
                (representative["id"], duplicate["id"]),
            )
            database.connection.execute(
                """
                INSERT INTO deliveries(
                    message_row_id, channel, delivery_type, batch_key, state,
                    available_at, created_at, updated_at, delivered_at
                ) VALUES(?, 'telegram', 'immediate', '', 'succeeded', ?, ?, ?, ?)
                """,
                (
                    representative["id"],
                    to_iso(utc_now()),
                    to_iso(utc_now()),
                    to_iso(utc_now()),
                    to_iso(utc_now()),
                ),
            )
        database.close()

        self.assertEqual(self.login().status_code, 200)
        default_rows = self.client.get(
            "/api/messages?q=cluster-audit&min_score=60"
        ).json()["items"]
        self.assertEqual([row["id"] for row in default_rows], [representative["id"]])
        self.assertEqual(default_rows[0]["similar_count"], 1)

        all_rows = self.client.get(
            "/api/messages?q=cluster-audit&min_score=60&similar_status=all"
        ).json()["items"]
        self.assertEqual({row["id"] for row in all_rows}, {representative["id"], duplicate["id"]})
        suppressed_rows = self.client.get(
            "/api/messages?q=cluster-audit&min_score=60&similar_status=suppressed"
        ).json()["items"]
        self.assertEqual([row["id"] for row in suppressed_rows], [duplicate["id"]])

        detail = self.client.get(f"/api/messages/{representative['id']}").json()
        self.assertEqual(detail["similar_cluster"]["similar_count"], 1)
        self.assertEqual(
            detail["similar_cluster"]["representative"]["id"], representative["id"]
        )
        self.assertEqual(
            [row["id"] for row in detail["similar_cluster"]["items"]],
            [duplicate["id"]],
        )
        self.assertNotIn(
            "semantic_dedupe_response_text",
            detail["similar_cluster"]["items"][0],
        )
        self.assertEqual(
            self.client.get("/api/messages?similar_status=invalid").status_code,
            422,
        )

    def test_unknown_watch_chat_is_rejected(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        response = self.client.put(
            "/api/config",
            headers=write_headers(),
            json={
                "important_keywords": ["紧急"],
                "trusted_sender_ids": [],
                "watch_chat_ids": [-1009999999999],
                "immediate_score": 60,
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_model_key_is_never_returned_and_blank_refresh_uses_saved_key(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        fetched = self.client.get("/api/model-config")
        self.assertEqual(fetched.status_code, 200)
        self.assertTrue(fetched.json()["api_key_configured"])
        self.assertEqual(fetched.json()["reasoning_effort"], "medium")
        self.assertEqual(fetched.json()["classification_model"], "test-model-z")
        self.assertEqual(fetched.json()["classification_reasoning_effort"], "high")
        self.assertEqual(fetched.json()["semantic_dedupe_model"], "test-dedupe-model")
        self.assertEqual(fetched.json()["semantic_dedupe_reasoning_effort"], "medium")
        self.assertEqual(fetched.json()["notification_model"], "test-notification-model")
        self.assertEqual(fetched.json()["notification_reasoning_effort"], "low")
        self.assertTrue(fetched.json()["community_insights_enabled"])
        self.assertTrue(fetched.json()["benefit_deals_enabled"])
        self.assertNotIn("api_key", fetched.json())
        self.assertNotIn(self.fake.api_key, json.dumps(fetched.json()))

        refreshed = self.client.post(
            "/api/model-config/models",
            headers=write_headers(),
            json={"base_url": self.fake.base_url, "api_key": ""},
        )
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.json()["items"], ["test-model-a", "test-model-z"])

    def test_model_probe_rejects_unsafe_url(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        unsafe = self.client.post(
            "/api/model-config/models",
            headers=write_headers(),
            json={"base_url": "file:///tmp/model.sock", "api_key": ""},
        )
        self.assertEqual(unsafe.status_code, 422)

    def test_model_save_never_echoes_key(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        response = self.client.put(
            "/api/model-config",
            headers=write_headers(),
            json={
                "enabled": True,
                "community_insights_enabled": False,
                "benefit_deals_enabled": False,
                "base_url": self.fake.base_url,
                "api_key": self.fake.api_key,
                "clear_api_key": False,
                "classification_model": "test-model-z",
                "classification_reasoning_effort": "low",
                "model": "test-model-a",
                "reasoning_effort": "high",
                "semantic_dedupe_model": "test-model-a",
                "semantic_dedupe_reasoning_effort": "default",
                "notification_model": "test-model-z",
                "notification_reasoning_effort": "medium",
            },
        )
        self.assertEqual(response.status_code, 200)
        serialized = json.dumps(response.json())
        self.assertNotIn("api_key", response.json())
        self.assertNotIn(self.fake.api_key, serialized)
        self.assertEqual(response.json()["reasoning_effort"], "high")
        self.assertEqual(response.json()["classification_model"], "test-model-z")
        self.assertEqual(response.json()["classification_reasoning_effort"], "low")
        self.assertEqual(response.json()["semantic_dedupe_model"], "test-model-a")
        self.assertEqual(response.json()["semantic_dedupe_reasoning_effort"], "default")
        self.assertEqual(response.json()["notification_model"], "test-model-z")
        self.assertEqual(response.json()["notification_reasoning_effort"], "medium")
        self.assertFalse(response.json()["community_insights_enabled"])
        self.assertFalse(response.json()["benefit_deals_enabled"])

    def test_model_save_rejects_invalid_reasoning_effort(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        response = self.client.put(
            "/api/model-config",
            headers=write_headers(),
            json={
                "enabled": True,
                "base_url": self.fake.base_url,
                "api_key": "",
                "clear_api_key": False,
                "model": "test-model-a",
                "reasoning_effort": "xhigh",
            },
        )
        self.assertEqual(response.status_code, 422)
        classification_response = self.client.put(
            "/api/model-config",
            headers=write_headers(),
            json={
                "enabled": True,
                "base_url": self.fake.base_url,
                "api_key": "",
                "clear_api_key": False,
                "classification_model": "test-model-z",
                "classification_reasoning_effort": "xhigh",
                "model": "test-model-a",
                "reasoning_effort": "default",
            },
        )
        self.assertEqual(classification_response.status_code, 422)
        for field in (
            "semantic_dedupe_reasoning_effort",
            "notification_reasoning_effort",
        ):
            payload = {
                "enabled": True,
                "base_url": self.fake.base_url,
                "api_key": "",
                "clear_api_key": False,
                "classification_model": "test-model-z",
                "classification_reasoning_effort": "low",
                "model": "test-model-a",
                "reasoning_effort": "default",
                "semantic_dedupe_model": "test-model-a",
                "semantic_dedupe_reasoning_effort": "low",
                "notification_model": "test-model-z",
                "notification_reasoning_effort": "low",
            }
            payload[field] = "xhigh"
            invalid = self.client.put(
                "/api/model-config",
                headers=write_headers(),
                json=payload,
            )
            self.assertEqual(invalid.status_code, 422)

    def test_push_config_is_protected_and_never_returns_secret_values(self) -> None:
        denied = self.client.get("/api/push-config")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(self.login().status_code, 200)
        fetched = self.client.get("/api/push-config")
        self.assertEqual(fetched.status_code, 200)
        payload = fetched.json()
        self.assertTrue(payload["telegram"]["enabled"])
        self.assertTrue(payload["telegram"]["bot_token_configured"])
        self.assertEqual(payload["ntfy"]["base_url"], "https://ntfy.example.com")
        self.assertNotIn("bot_token", payload["telegram"])
        self.assertNotIn("access_token", payload["ntfy"])
        self.assertNotIn("feedback_topic", payload["ntfy"])
        self.assertNotIn("feedback_signing_key", payload["ntfy"])
        self.assertTrue(payload["ntfy"]["feedback"]["topic"])
        serialized = json.dumps(payload)
        self.assertNotIn("test-bot-token", serialized)

    def test_push_config_save_validates_ntfy_and_never_echoes_tokens(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        saved = self.client.put(
            "/api/push-config",
            headers=write_headers(),
            json={
                "telegram_enabled": True,
                "telegram_bot_token": "",
                "clear_telegram_bot_token": False,
                "telegram_chat_id": "123456",
                "ntfy_enabled": True,
                "ntfy_base_url": "https://ntfy.example.com/",
                "ntfy_topic": "priority-news",
                "ntfy_community_topic": "priority-community",
                "ntfy_benefit_topic": "priority-benefits",
                "ntfy_access_token": "temporary-ntfy-test-token",
                "clear_ntfy_access_token": False,
            },
        )
        self.assertEqual(saved.status_code, 200)
        payload = saved.json()
        self.assertTrue(payload["ntfy"]["enabled"])
        self.assertTrue(payload["ntfy"]["access_token_configured"])
        self.assertTrue(payload["ntfy"]["feedback"]["enabled"])
        self.assertEqual(payload["ntfy"]["base_url"], "https://ntfy.example.com")
        self.assertEqual(payload["ntfy"]["topic"], "priority-news")
        self.assertEqual(payload["ntfy"]["community_topic"], "priority-community")
        self.assertEqual(payload["ntfy"]["benefit_topic"], "priority-benefits")
        self.assertNotIn("temporary-ntfy-test-token", json.dumps(payload))
        self.assertNotIn("access_token", payload["ntfy"])
        self.assertNotIn("feedback_topic", payload["ntfy"])
        self.assertNotIn("feedback_signing_key", payload["ntfy"])
        self.assertTrue(payload["ntfy"]["feedback"]["topic"])

        unsafe = self.client.put(
            "/api/push-config",
            headers=write_headers(),
            json={
                "telegram_enabled": True,
                "telegram_chat_id": "123456",
                "ntfy_enabled": True,
                "ntfy_base_url": "file:///tmp/ntfy.sock",
                "ntfy_topic": "priority-news",
            },
        )
        self.assertEqual(unsafe.status_code, 422)
        invalid_topic = self.client.put(
            "/api/push-config",
            headers=write_headers(),
            json={
                "telegram_enabled": True,
                "telegram_chat_id": "123456",
                "ntfy_enabled": True,
                "ntfy_base_url": "https://ntfy.example.com",
                "ntfy_topic": "topic/with/path",
            },
        )
        self.assertEqual(invalid_topic.status_code, 422)

    def test_push_channel_test_uses_saved_secret_server_side(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        with patch(
            "app.web.send_push_test", new=AsyncMock(return_value=True)
        ) as sender:
            response = self.client.post(
                "/api/push-config/test",
                headers=write_headers(),
                json={"channel": "telegram"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"channel": "telegram", "sent": True})
        private_config = sender.await_args.args[0]
        self.assertEqual(sender.await_args.args[1], "telegram")
        self.assertEqual(private_config["telegram"]["bot_token"], "test-bot-token")
        self.assertNotIn("test-bot-token", response.text)

    def test_manual_reanalysis_and_duplicate_pending(self) -> None:
        self.assertEqual(self.login().status_code, 200)
        before = len(self.fake.requests)
        accepted = self.client.post(
            f"/api/messages/{self.message_row_id}/reanalyze",
            headers=write_headers(),
            json={},
        )
        self.assertEqual(accepted.status_code, 202)
        value = accepted.json()
        self.assertEqual(value["ai_status"], "queued")
        self.assertEqual(value["analysis_queue_state"], "queued")
        self.assertTrue(value["analysis_queue_manual"])
        self.assertTrue(value["queue_created"])
        self.assertIsInstance(value["analysis_job_id"], int)
        self.assertFalse(value["push_eligible"])
        self.assertEqual(value["push_gate_reason"], "manual_reanalysis_queued")
        post_requests = [
            item
            for item in self.fake.requests[before:]
            if item["method"] == "POST" and item["path"] == "/v1/chat/completions"
        ]
        self.assertEqual(post_requests, [])

        listed = self.client.get("/api/messages").json()["items"]
        matching = next(row for row in listed if row["id"] == self.message_row_id)
        self.assertEqual(matching["analysis_queue_state"], "queued")
        self.assertNotIn("ai_response_text", matching)
        self.assertNotIn("ai_category_response_text", matching)
        self.assertNotIn("semantic_dedupe_response_text", matching)

        duplicate = self.client.post(
            f"/api/messages/{self.message_row_id}/reanalyze",
            headers=write_headers(),
            json={},
        )
        self.assertEqual(duplicate.status_code, 202)
        self.assertFalse(duplicate.json()["queue_created"])
        self.assertEqual(duplicate.json()["analysis_job_id"], value["analysis_job_id"])


if __name__ == "__main__":
    unittest.main()
