"""真实 SDK 的本机协议测试服务；固定响应只检查控制流，不模拟模型智能。"""

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from openai import DefaultHttpxClient, OpenAI


def completion(*, path=None, arguments=None, text="", finish="stop", name="read_file", count=1):
    """固定的协议响应只用于检查控制流，不模拟模型的推理能力。"""
    message = {"role": "assistant", "content": text}
    if path is not None or arguments is not None:
        message["tool_calls"] = [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments if arguments is not None else {"path": path}),
                },
            }
            for i in range(count)
        ]
    return {
        "id": "local",
        "object": "chat.completion",
        "created": 0,
        "model": "local",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }


@contextmanager
def local_api(responses):
    """让实际 SDK 访问本机服务，记录它真正发出的请求与消息续接。"""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            response = responses[min(len(requests) - 1, len(responses) - 1)]
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with OpenAI(
            api_key="local-protocol-test",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            max_retries=0,
            timeout=2,
            http_client=DefaultHttpxClient(trust_env=False),
        ) as client:
            yield client, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
