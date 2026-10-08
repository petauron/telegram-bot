from __future__ import annotations

import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeOpenAIServer:
    def __init__(self, *, api_key: str | None = None) -> None:
        self.api_key = api_key or secrets.token_urlsafe(24)
        self.models = ["test-model-z", "test-model-a", "test-model-a"]
        self.classification_content = json.dumps(
            {
                "category": "external_information",
                "confidence": 93,
                "summary": "本地假服务分类摘要",
                "reason": "消息描述了可供参考的外部事件",
            },
            ensure_ascii=False,
        )
        self.batch_classification_content: str | None = None
        self.scoring_content = json.dumps(
            {
                "score": 88,
                "summary": "本地假服务摘要",
                "reason": "本地假服务评分理由",
            },
            ensure_ascii=False,
        )
        self.community_content = json.dumps(
            {
                "valuable": True,
                "signal_type": "incident_report",
                "confidence": 88,
                "score": 76,
                "title": "社区反馈某服务连接异常",
                "summary": "讨论中出现具体连接失败现象，具有排障参考价值。",
                "reason": "消息包含明确对象、现象和实际影响",
                "evidence_count": 1,
            },
            ensure_ascii=False,
        )
        self.benefit_content = json.dumps(
            {
                "valuable": True,
                "benefit_type": "official_freebie",
                "confidence": 92,
                "score": 82,
                "title": "开发工具开放限时免费额度",
                "summary": "指定开发工具向符合条件的用户开放限时免费额度，领取条件与期限明确。",
                "reason": "消息包含对象、具体福利、适用条件和有效期",
            },
            ensure_ascii=False,
        )
        self.dedupe_content = json.dumps(
            {
                "same_event": False,
                "match_index": None,
                "confidence": 96,
                "material_update": False,
                "update_type": "none",
                "reason": "本地假服务判断为不同事件",
            },
            ensure_ascii=False,
        )
        self.notification_content = json.dumps(
            {
                "title": "本地假服务整理标题",
                "body": "本地假服务整理后的简洁客户正文。",
            },
            ensure_ascii=False,
        )
        self.chat_status = 200
        self.classification_status = 200
        self.scoring_status = 200
        self.community_status = 200
        self.benefit_status = 200
        self.dedupe_status = 200
        self.notification_status = 200
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _: str, *args: object) -> None:
                del args

            def _authorized(self) -> bool:
                return secrets.compare_digest(
                    self.headers.get("Authorization", ""),
                    f"Bearer {owner.api_key}",
                )

            def _json_response(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    self._json_response(401, {"error": "unauthorized"})
                    return
                owner.requests.append({"method": "GET", "path": self.path, "authorized": True})
                if self.path == "/v1/models":
                    self._json_response(200, {"data": [{"id": model} for model in owner.models]})
                    return
                self._json_response(404, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    self._json_response(401, {"error": "unauthorized"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                    self._json_response(400, {"error": "invalid_json"})
                    return
                owner.requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "authorized": True,
                        "payload": payload,
                    }
                )
                if self.path != "/v1/chat/completions":
                    self._json_response(404, {"error": "not_found"})
                    return
                system_content = str(payload.get("messages", [{}])[0].get("content", ""))
                if "语义去重阶段" in system_content:
                    stage = "dedupe"
                elif "内容整理阶段" in system_content:
                    stage = "notification"
                elif "社区线索提取阶段" in system_content:
                    stage = "community"
                elif "福利羊毛筛选阶段" in system_content:
                    stage = "benefit"
                elif "分类阶段" in system_content:
                    stage = "classification"
                else:
                    stage = "scoring"
                owner.requests[-1]["stage"] = stage
                stage_status = {
                    "classification": owner.classification_status,
                    "scoring": owner.scoring_status,
                    "community": owner.community_status,
                    "benefit": owner.benefit_status,
                    "dedupe": owner.dedupe_status,
                    "notification": owner.notification_status,
                }[stage]
                status = owner.chat_status if owner.chat_status != 200 else stage_status
                if status != 200:
                    self._json_response(status, {"error": "simulated"})
                    return
                classification_content = owner.classification_content
                user_content = str(payload.get("messages", [{}, {}])[-1].get("content", ""))
                try:
                    user_payload = json.loads(user_content)
                except json.JSONDecodeError:
                    user_payload = {}
                batch_messages = user_payload.get("batch_messages")
                if stage == "classification" and isinstance(batch_messages, list):
                    if owner.batch_classification_content is not None:
                        classification_content = owner.batch_classification_content
                    else:
                        try:
                            template = json.loads(owner.classification_content)
                        except json.JSONDecodeError:
                            template = None
                        if isinstance(template, dict):
                            classification_content = json.dumps(
                                {
                                    "results": [
                                        {
                                            "message_row_id": item.get("message_row_id"),
                                            "message_id": item.get("message_id"),
                                            **template,
                                        }
                                        for item in batch_messages
                                        if isinstance(item, dict)
                                    ]
                                },
                                ensure_ascii=False,
                            )
                content = {
                    "classification": classification_content,
                    "scoring": owner.scoring_content,
                    "community": owner.community_content,
                    "benefit": owner.benefit_content,
                    "dedupe": owner.dedupe_content,
                    "notification": owner.notification_content,
                }[stage]
                self._json_response(
                    200,
                    {
                        "choices": [
                            {"message": {"role": "assistant", "content": content}}
                        ]
                    },
                )

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def start(self) -> "FakeOpenAIServer":
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def main() -> None:
    api_key = os.environ.get("FAKE_API_KEY")
    if not api_key:
        raise SystemExit("FAKE_API_KEY is required")
    server = FakeOpenAIServer(api_key=api_key).start()
    print(f"READY {server.base_url}", flush=True)
    try:
        server._thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


if __name__ == "__main__":
    main()
