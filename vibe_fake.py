"""Локальный двойник Agent API: отладка агента без списаний.

Зачем. У платформы нет песочницы и нет отмены задачи. Значит любая отладка
интеграции идёт за живые деньги: перепутал поле — заплатил, промахнулся
моделью — заплатил, проверил обработку ошибки 429 — а как её вызвать, кроме
как реально упереться в лимит.

Этот сервер повторяет контракт: те же пути, те же коды ошибок, тот же
асинхронный цикл со статусами, те же подписанные вебхуки. Разработчик гоняет
на нём свой код, а на боевой ключ переходит, когда всё уже работает.

    python vibe_fake.py --port 8777
    VIBE_TOKEN=любой python -c "from vibe import Vibe; \
        print(Vibe('t', base='http://127.0.0.1:8777/api/agent').balance())"

Чем он специально плох: генерирует не картинки, а заглушки. Задача сервера —
воспроизвести поведение API, а не нейросетей.

Управление сценариями через заголовок X-Fake: rate_limit, no_balance,
slow, fail. Так проверяются ветки, до которых на боевом API не добраться.

Проверка: python vibe_fake.py --selftest
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = "fake-webhook-secret"

# Схемы моделей повторяют форму живого /capabilities: имя, тип, параметры, цена.
MODELS = [
    {"model": "z-image", "type": "image", "params": ["prompt", "aspect_ratio"], "price": 1.2},
    {"model": "seedream-5-pro-edit", "type": "image",
     "params": ["prompt", "image_input", "quality"], "price": 9},
    {"model": "grok-itv-10", "type": "video",
     "params": ["prompt", "image_urls", "duration", "resolution"], "price": 36},
    {"model": "omnihuman-1-5", "type": "video",
     "params": ["prompt", "image_url", "audio_url", "resolution"], "price": 96},
    {"model": "el-tts-multilingual-v2", "type": "voice",
     "params": ["prompt", "voice_id", "stability", "speed"], "price": 39},
    {"model": "claude-opus-5", "type": "text",
     "params": ["prompt", "system", "max_tokens", "effort", "thinking"], "price": 4},
]
BY_NAME = {m["model"]: m for m in MODELS}

# Живой каталог отдаёт models вложенным словарём: тип -> имя -> схема.
# Двойник обязан повторять именно эту форму, иначе клиент против него читает
# пустой каталог и его проверка имени модели молча выключается.
CAPS = {"models": {}}
for _m in MODELS:
    CAPS["models"].setdefault(_m["type"], {})[_m["model"]] = {
        "price": _m["price"],
        "required": ["prompt"],
        "optional": [p for p in _m["params"] if p != "prompt"],
    }
COMMON = {"type", "model", "prompt", "strict", "idempotency_key", "callback_url", "brand_id"}


class State:
    """Общее состояние сервера. Отдельным классом, чтобы тесты могли поднять
    несколько независимых серверов в одном процессе."""

    def __init__(self, balance=1000.0):
        self.balance = balance
        self.jobs = {}
        self.seen_idem = {}
        self.next_id = 1000
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    state = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass                                    # не засорять консоль отладки

    # ---------- маршруты ----------

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith("/capabilities"):
            return self._ok(CAPS)
        if path.endswith("/balance"):
            return self._ok({"balance": round(self.state.balance, 2)})
        if path.endswith("/me"):
            return self._ok({"token": "fake", "scopes": ["read", "generate"],
                             "balance": round(self.state.balance, 2)})
        # отдаём и сам файл: клиент обязан скачивать результат, пока ссылка жива,
        # значит двойник должен давать что скачивать
        m = re.search(r"/files/generation/(\d+)", path)
        if m:
            blob = f"двойник Agent API, заглушка результата генерации {m.group(1)}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return True

        m = re.search(r"/generation/(\d+)/status", path)
        if m:
            job = self.state.jobs.get(int(m.group(1)))
            if not job:
                return self._err(404, "not_found", "генерации с таким id нет")
            return self._ok(self._view(job))
        return self._err(404, "not_found", "метод не найден")

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read()
        scenario = self.headers.get("X-Fake", "")

        if not self.headers.get("Authorization", "").startswith("Bearer "):
            return self._err(401, "missing_token", "нет заголовка Authorization")
        if scenario == "rate_limit":
            return self._err(429, "rate_limit_exceeded", "слишком часто", retry_after=2)
        if scenario == "no_balance":
            return self._err(402, "insufficient_balance", "не хватает рублей")

        if path.endswith("/generate/estimate"):
            check = self._validate(body)
            if check:
                return check
            return self._ok({"valid": True, "dry_run": True,
                             "estimated_cost_rub": self._price(body),
                             "balance": {"current": round(self.state.balance, 2)}})
        if path.endswith("/generate"):
            return self._generate(body, scenario)
        return self._err(404, "not_found", "метод не найден")

    # ---------- логика ----------

    def _validate(self, body):
        """Повторяет pre-charge проверку платформы: всё, что можно отклонить
        до списания, отклоняется до списания."""
        for field in ("type", "model", "prompt"):
            if not body.get(field):
                return self._err(422, "validation_failed", f"поле {field} обязательно",
                                 details={field: ["обязательное поле"]})
        spec = BY_NAME.get(body["model"])
        if not spec:
            return self._err(422, "model_not_supported",
                             f"модель {body['model']} не найдена",
                             details={"known": sorted(BY_NAME)})
        if spec["type"] != body["type"]:
            return self._err(422, "validation_failed",
                             f"модель {spec['model']} работает с type={spec['type']}")
        extra = set(body) - COMMON - set(spec["params"])
        if extra:
            if body.get("strict"):
                return self._err(422, "unknown_or_incompatible_params",
                                 "поля не поддерживаются этой моделью",
                                 rejected=sorted(extra), valid_params=spec["params"])
            # без strict платформа берёт деньги и молча выкидывает поля,
            # ровно на этом теряются рубли; двойник ведёт себя так же
            return None
        return None

    def _price(self, body):
        spec = BY_NAME.get(body["model"], {"price": 1})
        price = spec["price"]
        if body.get("duration"):
            price = price * float(body["duration"]) / 5
        return round(price, 2)

    def _generate(self, body, scenario):
        check = self._validate(body)
        if check:
            return check

        idem = body.get("idempotency_key")
        with self.state.lock:
            if idem and idem in self.state.seen_idem:
                prev = dict(self.state.seen_idem[idem])
                prev["replayed"] = True
                return self._ok(prev)              # повтор не оплачивается второй раз

            cost = self._price(body)
            if cost > self.state.balance:
                return self._err(402, "insufficient_balance",
                                 "не хватает рублей", required=cost,
                                 balance=round(self.state.balance, 2))
            self.state.balance -= cost
            gid = self.state.next_id
            self.state.next_id += 1
            spec = BY_NAME[body["model"]]
            ignored = sorted(set(body) - COMMON - set(spec["params"]))
            job = {"id": gid, "type": body["type"], "model": body["model"], "cost": cost,
                   "ready_at": time.time() + (6 if scenario == "slow" else 1),
                   "fail": scenario == "fail", "callback": body.get("callback_url"),
                   "ignored": ignored}
            self.state.jobs[gid] = job

            if body["type"] == "text":             # текст платформа отдаёт синхронно
                out = {"status": "complete", "generation_id": gid, "type": "text",
                       "model": body["model"], "text": f"[двойник] ответ на: {body['prompt'][:60]}",
                       "usage": {"input": 100, "output": 40}, "cost": cost,
                       "balance_after": round(self.state.balance, 2)}
            else:
                out = {"status": "processing", "generation_id": gid, "task_id": f"task-{gid}",
                       "cost": cost, "balance_after": round(self.state.balance, 2)}
            if ignored:
                out["ignored_params"] = ignored    # деньги ушли, поля не применились
            # запись ключа под тем же локом, что и списание: иначе между
            # освобождением лока и записью проходит второй запрос и платит ещё раз
            if idem:
                self.state.seen_idem[idem] = out

        if job["callback"]:
            threading.Thread(target=self._fire_webhook, args=(job,), daemon=True).start()
        return self._ok(out)

    def _view(self, job):
        if time.time() < job["ready_at"]:
            return {"status": "processing", "generation_id": job["id"], "type": job["type"]}
        if job["fail"]:
            with self.state.lock:
                if not job.get("refunded"):
                    self.state.balance += job["cost"]
                    job["refunded"] = True
            return {"status": "error", "generation_id": job["id"], "type": job["type"],
                    "error_message": "провайдер не ответил", "refunded": True}
        return {"status": "complete", "generation_id": job["id"], "type": job["type"],
                "model": job["model"], "cost": job["cost"],
                "display_url": f"http://{self.headers.get('Host', 'localhost')}"
                               f"/files/generation/{job['id']}.bin",
                "expires_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                            time.gmtime(time.time() + 7 * 86400))}

    def _fire_webhook(self, job):
        """Вебхук с подписью тела, как на боевом API: HMAC-SHA256 сырых байт."""
        time.sleep(max(0.0, job["ready_at"] - time.time()) + 0.2)
        payload = {"event": "generation.error" if job["fail"] else "generation.complete",
                   "generation_id": job["id"], "type": job["type"], "model": job["model"],
                   "cost": job["cost"], "attempt": 1}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        req = urllib.request.Request(job["callback"], data=raw, method="POST", headers={
            "Content-Type": "application/json", "X-Vibe-Event": payload["event"],
            "X-Vibe-Signature": sig, "X-Vibe-Generation": str(job["id"])})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass                                   # боевой API тоже молча отступает

    # ---------- ответы ----------

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def _send(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # _ok и _err возвращают True: вызывающий по этому признаку понимает, что
    # ответ уже ушёл, и прекращает работу. Без этого валидация отправляла 422
    # и всё равно шла списывать деньги.
    def _ok(self, payload):
        self._send(200, payload)
        return True

    def _err(self, status, code, message, **extra):
        self._send(status, {"status": "error", "error": code, "message": message,
                            "request_id": f"fake-{int(time.time() * 1000)}", **extra})
        return True


def serve(port=8777, balance=1000.0):
    """Поднимает сервер. Возвращает (server, base_url), останавливать shutdown()."""
    handler = type("BoundHandler", (Handler,), {"state": State(balance)})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/api/agent"


def verify(raw_body: bytes, signature: str, secret: str = SECRET) -> bool:
    """Проверка подписи вебхука. Считать надо по сырым байтам ДО разбора JSON:
    после json.loads порядок ключей и пробелы уже не те, подпись не сойдётся."""
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _selftest():
    from vibe import Vibe, VibeError

    srv, base = serve(port=0)
    try:
        v = Vibe("test-token", base=base, max_retries=2)

        # 1. Сквозной путь: запуск, ожидание, готовый результат.
        g = v.generate("image", "z-image", "закат над рекой")
        done = v.wait(g, timeout=30, poll=1)
        assert done["status"] == "complete", done
        assert done["display_url"].endswith(".bin"), done

        # 2. Опечатка в поле не превращается в списание: strict ловит до денег.
        before = v.balance()["balance"]
        try:
            v.generate("image", "z-image", "закат", aspekt_ratio="16:9")
            raise AssertionError("опечатка прошла и оплатилась")
        except VibeError as e:
            assert e.status == 422 and e.code == "unknown_or_incompatible_params", e.payload
        assert v.balance()["balance"] == before, "деньги списались на отклонённом запросе"

        # 3. Клиент кладёт картинку в поле нужной модели, сервер её принимает.
        g = v.generate("video", "grok-itv-10", "кот прыгает",
                       image="https://example.com/cat.png", duration=5)
        assert v.wait(g, timeout=30, poll=1)["status"] == "complete"

        # 4. Повтор с тем же ключом идемпотентности не списывает дважды.
        before = v.balance()["balance"]
        body = {"type": "image", "model": "z-image", "prompt": "дубль", "strict": True}
        a = v._call("POST", "/generate", dict(body), idem="fixed-key")
        b = v._call("POST", "/generate", dict(body), idem="fixed-key")
        assert b.get("replayed") and a["generation_id"] == b["generation_id"], (a, b)
        spent = before - v.balance()["balance"]
        assert abs(spent - a["cost"]) < 0.01, f"списано {spent} вместо {a['cost']}"

        # 5. Ошибка генерации возвращает деньги и не выглядит успехом.
        before = v.balance()["balance"]
        normal = v._transport                      # вернуть обратно, иначе падать будет всё дальше
        v._transport = _with_header(normal, "fail")
        try:
            g = v.generate("image", "z-image", "заведомо провальная")
            try:
                v.wait(g, timeout=30, poll=1)
                raise AssertionError("провал генерации принят за успех")
            except VibeError as e:
                assert e.payload.get("refunded") is True, e.payload
        finally:
            v._transport = normal
        assert abs(v.balance()["balance"] - before) < 0.01, "возврат не пришёл"

        # 5б. save() принимает и готовый ответ, и голый id генерации.
        import os
        import tempfile
        g = v.generate("image", "z-image", "для сохранения")
        with tempfile.TemporaryDirectory() as tmp:
            paths = v.save(g["generation_id"], tmp)
            assert paths and os.path.exists(paths[0]), paths

        # 5в. Каталог двойника имеет ту же форму, что живой, поэтому проверка
        #     имени модели срабатывает У КЛИЕНТА, до похода на сервер.
        assert v.known_models("image"), "клиент читает каталог двойника как пустой"
        try:
            v.generate("image", "z-imagee", "опечатка в имени модели")
            raise AssertionError("несуществующая модель ушла на сервер")
        except VibeError as e:
            assert e.code == "model_not_in_catalog", e.payload
            assert "z-image" in e.payload["message"], e.payload["message"]

        # 6. Подпись вебхука сходится только на неизменённом теле.
        raw = b'{"event":"generation.complete","generation_id":1}'
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        assert verify(raw, sig)
        assert not verify(raw + b" ", sig)

        print("самопроверка двойника пройдена: 6 из 6")
    finally:
        srv.shutdown()


def _with_header(transport, value):
    def wrapped(method, url, body, headers):
        return transport(method, url, body, dict(headers or {}, **{"X-Fake": value}))
    return wrapped


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="локальный двойник Agent API")
    p.add_argument("--port", type=int, default=8777)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        _selftest()
    else:
        srv, base = serve(a.port, a.balance)
        print(f"двойник поднят: {base}\nбаланс: {a.balance} р\nCtrl+C для остановки")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            srv.shutdown()
