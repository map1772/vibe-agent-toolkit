"""Клиент Agent API Вайб-Маркетолога с предохранителем.

Главная мысль: в этом API ошибка стоит денег, а не 400-й ошибки. Поле
названо не так — запрос выполнится «пустым» вариантом, и рубли спишутся.
Документация сама называет это самой частой ошибкой агентов.

Поэтому клиент устроен наоборот обычного SDK: он старается НЕ отправить
запрос, пока не убедится, что тот сделает ровно то, что просили.

    from vibe import Vibe

    v = Vibe(token)
    g = v.generate("video", "grok-itv-10", "кот прыгает", image="https://.../cat.png")
    v.save(g, "out/")          # результат живёт 7 дней, забираем сразу

Что делает клиент, чего не делает голый requests:
  * кладёт картинку в то поле, которое понимает конкретная модель
    (image_urls / image_url / first_frame_url / character_image_url / image_input);
  * шлёт strict=true, поэтому опечатка в поле возвращает 422 ДО списания;
  * держит idempotency_key на время ретраев: сетевой сбой не оплачивается дважды;
  * уважает retry_after и не загоняет ключ в key_cooling_down;
  * скачивает результат, пока ссылка жива.

Проверка без сети: python vibe.py --selftest
"""
from __future__ import annotations

import difflib
import json
import os
import time
import urllib.error
import urllib.request
import uuid

BASE = "https://lk.vibemarketolog.ru/api/agent"

# Куда каждая модель ждёт входную картинку/видео/аудио. Таблица нужна как
# фолбэк: живой /capabilities точнее, но он есть не всегда (офлайн, старый ключ).
# Источник: раздел «image-to-video: правильное поле под модель» документации.
MEDIA_FIELDS = {
    "image": [
        "image_urls",            # grok-itv, veo3, kling, gemini-omni-video
        "image_url",             # omnihuman-1-5, единственное число
        "character_image_url",   # motion-control
        "first_frame_url",       # seedance
        "reference_image_urls",  # seedance, референсы
        "image_input",           # image-модели, редактирование
    ],
    "video": ["video_url", "reference_video_url", "reference_video_urls"],
    "audio": ["audio_url", "reference_audio_urls"],
}

# Модели, которым мало положить картинку: нужен ещё переключатель режима.
MODE_SWITCH = {
    "veo3": {"generation_type": "image-to-video"},
    "veo3.1": {"generation_type": "image-to-video"},
    "veo3_fast": {"generation_type": "image-to-video"},
}

RETRY_STATUS = {429, 500, 502, 503, 504}


class VibeError(Exception):
    """Ошибка API. Код и тело сохранены, чтобы вызывающий мог их разобрать."""

    def __init__(self, status, payload):
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {"raw": payload}
        self.code = self.payload.get("error", "http_error")
        msg = self.payload.get("message") or self.payload.get("raw") or ""
        super().__init__(f"[{status} {self.code}] {msg}")


class Vibe:
    def __init__(self, token=None, base=BASE, timeout=90, max_retries=5,
                 confirm_above=None, transport=None):
        """confirm_above: рубли. Дороже этой суммы клиент сначала спросит смету
        и откажется запускать без confirm=True. Ноль отключает проверку."""
        self.token = token or os.environ.get("VIBE_TOKEN", "")
        if not self.token:
            raise ValueError("нужен токен: Vibe(token) или переменная VIBE_TOKEN")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.confirm_above = confirm_above
        self._transport = transport or self._http     # подмена в тестах
        self._caps = None

    # ---------- транспорт ----------

    def _http(self, method, url, body=None, headers=None):
        data = None
        headers = dict(headers or {})
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw or "{}")
            except json.JSONDecodeError:
                return e.code, {"raw": raw}

    def _call(self, method, path, body=None, idem=None):
        """Один запрос с ретраями. Ключ идемпотентности держим постоянным между
        попытками: иначе повтор после таймаута сети оплачивается второй раз."""
        url = f"{self.base}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        if idem:
            body = dict(body or {}, idempotency_key=idem)
        delay = 1.0
        status, payload = 0, {}
        for attempt in range(max(1, self.max_retries)):
            status, payload = self._transport(method, url, body, headers)
            if status < 400:
                return payload
            if status not in RETRY_STATUS or attempt == self.max_retries - 1:
                raise VibeError(status, payload)
            # сервер сам говорит, сколько ждать; свои догадки тут вредны
            wait = payload.get("retry_after")
            time.sleep(float(wait) if wait else delay)
            delay = min(delay * 2, 30)
        raise VibeError(status, payload)

    # ---------- справочники ----------

    @property
    def capabilities(self):
        if self._caps is None:
            self._caps = self._call("GET", "/capabilities")
        return self._caps

    def model_spec(self, model):
        """Описание модели из живого каталога. None, если каталог недоступен
        или модели в нём нет: тогда работаем по таблице из документации."""
        try:
            caps = self.capabilities
        except VibeError:
            return None
        return _find_model(caps, model)

    def balance(self):
        return self._call("GET", "/balance")

    def estimate(self, type, model, prompt, **fields):
        body = self._body(type, model, prompt, fields)
        return self._call("POST", "/generate/estimate", body)

    # ---------- сборка запроса ----------

    def known_models(self, type=None):
        """Имена моделей из живого каталога. Пустое множество, если каталог
        недоступен: тогда проверять нечем и клиент не мешает работать."""
        try:
            caps = self.capabilities
        except VibeError:
            return set()
        models = caps.get("models")
        if not isinstance(models, dict):
            return set()
        if type:
            node = models.get(type)
            return set(node) if isinstance(node, dict) else set()
        out = set()
        for node in models.values():
            if isinstance(node, dict):
                out |= set(node)
        return out

    def _body(self, type, model, prompt, fields):
        fields = dict(fields)

        # Документация и живой каталог расходятся: в доках grok-itv-10, в
        # каталоге grok-itv, а omnihuman-1-5 описан с ценой, но не отдаётся
        # вовсе. Ловим это здесь, а не по 422 после отправки.
        known = self.known_models(type)
        if known and model not in known:
            near = difflib.get_close_matches(model, sorted(known), n=3, cutoff=0.5)
            raise VibeError(0, {
                "error": "model_not_in_catalog",
                "message": f"модели {model} нет в каталоге типа {type}"
                           + (f", возможно вы имели в виду: {', '.join(near)}" if near else ""),
                "known": sorted(known)})

        body = {"type": type, "model": model, "prompt": prompt, "strict": True}

        # человеческие имена -> поле конкретной модели
        for kind in ("image", "video", "audio"):
            value = fields.pop(kind, None)
            if value is None:
                continue
            target = self._media_field(model, kind)
            if not target:
                raise ValueError(
                    f"модель {model} не принимает {kind}: в её схеме нет подходящего поля. "
                    f"Проверьте GET /capabilities или уберите аргумент {kind}=")
            body[target] = _shape(target, value)
            body.update(MODE_SWITCH.get(model, {}))

        body.update(fields)
        body["strict"] = True     # после merge: иначе strict=False из kwargs тихо снимет защиту
        return {k: v for k, v in body.items() if v is not None}

    def _media_field(self, model, kind):
        """Ищем приёмник медиа сначала в живой схеме модели, потом в таблице."""
        spec = self.model_spec(model)
        if spec is None:                       # каталог недоступен или модель новая
            return _fallback_field(model, kind)
        known = _param_names(spec)
        return next((n for n in MEDIA_FIELDS[kind] if n in known), None)

    # ---------- генерация ----------

    def generate(self, type, model, prompt, confirm=False, **fields):
        """Запускает генерацию. Возвращает ответ API (для type=text он уже
        содержит текст, для остальных типов — generation_id)."""
        body = self._body(type, model, prompt, fields)

        if self.confirm_above and not confirm:
            est = self._call("POST", "/generate/estimate", body)
            cost = float(est.get("estimated_cost_rub") or est.get("reserve_rub") or 0)
            if cost > self.confirm_above:
                raise VibeError(0, {
                    "error": "confirm_required",
                    "message": f"смета {cost} р выше порога {self.confirm_above} р, "
                               f"повторите вызов с confirm=True",
                    "estimate": est})

        return self._call("POST", "/generate", body, idem=str(uuid.uuid4()))

    def status(self, generation_id):
        return self._call("GET", f"/generation/{generation_id}/status")

    def wait(self, generation, timeout=1800, poll=12):
        """Ждёт готовности. Опрашивает не чаще раза в 12 секунд: частый поллинг
        съедает лимит 120 запросов в минуту и ничего не ускоряет."""
        gid = generation.get("generation_id") if isinstance(generation, dict) else generation
        if not gid:
            return generation                      # type=text отдаёт результат сразу
        deadline = time.time() + timeout
        while True:
            st = self.status(gid)
            state = st.get("status")
            if state == "complete":
                return st
            if state == "error":
                raise VibeError(0, {"error": "generation_failed",
                                    "message": st.get("error_message", ""),
                                    "refunded": st.get("refunded"), "status": st})
            if time.time() > deadline:
                raise TimeoutError(f"генерация {gid} не закончилась за {timeout} с")
            time.sleep(poll)

    def save(self, generation, folder="."):
        """Скачивает результат на диск. Ссылки живут 7 дней, поэтому забирать
        файл сразу — не перестраховка, а единственный способ его сохранить."""
        # принимаем и готовый ответ, и просто id: вызывающему не надо помнить, что у него в руках
        if not isinstance(generation, dict):
            generation = self.status(generation)
        st = generation if generation.get("status") == "complete" else self.wait(generation)
        urls = st.get("result_urls") or [st.get("display_url") or st.get("result_url")]
        urls = [u for u in urls if u]
        if not urls:
            raise VibeError(0, {"error": "no_result", "message": "в ответе нет ссылок на файл"})
        os.makedirs(folder, exist_ok=True)
        saved = []
        for i, url in enumerate(urls):
            name = f"{st.get('generation_id', 'gen')}{'' if len(urls) == 1 else f'_{i}'}"
            path = os.path.join(folder, name + _ext(url, st.get("type")))
            with urllib.request.urlopen(url, timeout=self.timeout) as r, open(path, "wb") as f:
                f.write(r.read())
            saved.append(path)
        return saved


# ---------- разбор каталога ----------

def _find_model(caps, model):
    """Каталог самоописывающийся, но его форма не зафиксирована в документации.
    Поэтому ищем модель по всему дереву, а не по одному ожидаемому пути."""
    stack = [caps]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("model") == model or node.get("name") == model or node.get("key") == model:
                return node
            if model in node and isinstance(node[model], dict):
                return node[model]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _param_names(spec):
    """Имена параметров модели, в каком бы виде каталог их ни отдал."""
    names = set()
    for key in ("params", "parameters", "fields", "schema", "properties",
                "required", "optional"):
        node = spec.get(key)
        if isinstance(node, dict):
            names |= set(node.keys())
        elif isinstance(node, list):
            names |= {x if isinstance(x, str) else str(x.get("name", "")) for x in node}
    return {n for n in names if n}


def _fallback_field(model, kind):
    """Поле по таблице документации, когда живого каталога нет."""
    m = model.lower()
    if kind == "image":
        if m.startswith("omnihuman"):
            return "image_url"
        if m.startswith("motion-control"):
            return "character_image_url"
        if m.startswith("seedance"):
            return "first_frame_url"
        if m.startswith(("gpt-image", "nano-banana", "seedream", "z-image", "grok-image")):
            return "image_input"
        return "image_urls"
    if kind == "video":
        if m.startswith("motion-control"):
            return "reference_video_url"
        return "video_url"
    return "audio_url"


def _shape(field, value):
    """Половина полей ждёт список, половина одну строку. Приводим по имени."""
    plural = field.endswith("s")
    if isinstance(value, (list, tuple)):
        return list(value) if plural else value[0]
    return [value] if plural else value


def _ext(url, type_hint=None):
    # расширение ищем только в последнем сегменте пути: точки в домене не в счёт
    name = url.split("?")[0].rsplit("/", 1)[-1]
    tail = name.rsplit(".", 1)
    if len(tail) == 2 and 1 <= len(tail[1]) <= 5 and tail[1].isalnum():
        return "." + tail[1]
    return {"video": ".mp4", "image": ".png", "voice": ".mp3", "music": ".mp3"}.get(type_hint, "")


# ---------- самопроверка без сети ----------

def _selftest():
    calls = []

    def fake(method, url, body, headers):
        calls.append({"method": method, "url": url, "body": body})
        if url.endswith("/capabilities"):
            # форма как у живого каталога: models -> тип -> имя -> {required, optional}
            return 200, {"models": {
                "video": {
                    "grok-itv-10": {"required": ["prompt"], "optional": ["image_urls", "duration"]},
                    "omnihuman-1-5": {"required": ["prompt", "image_url", "audio_url"], "optional": []},
                },
                "image": {
                    "seedream-5-pro-edit": {"required": ["prompt"], "optional": ["image_input"]},
                    "z-image": {"required": ["prompt"], "optional": ["aspect_ratio"]},
                },
            }}
        if url.endswith("/generate/estimate"):
            return 200, {"valid": True, "estimated_cost_rub": 240}
        if url.endswith("/generate"):
            return 200, {"status": "processing", "generation_id": 777, "cost": 36}
        if "/status" in url:
            return 200, {"status": "complete", "generation_id": 777, "type": "video",
                         "display_url": "https://host/files/generation/777.mp4"}
        return 404, {"error": "not_found"}

    v = Vibe("test-token", transport=fake)

    # 1. Картинка ложится в поле, которое понимает именно эта модель.
    body = v._body("video", "grok-itv-10", "кот", {"image": "https://h/cat.png"})
    assert body["image_urls"] == ["https://h/cat.png"], body
    body = v._body("video", "omnihuman-1-5", "речь", {"image": "https://h/face.png"})
    assert body["image_url"] == "https://h/face.png", body   # единственное число, не список
    body = v._body("image", "seedream-5-pro-edit", "правка", {"image": "https://h/a.png"})
    assert body["image_input"] == "https://h/a.png", body

    # 2. Модель без входной картинки не принимает её молча.
    try:
        v._body("image", "z-image", "закат", {"image": "https://h/a.png"})
        raise AssertionError("картинку приняли там, где модель её не читает")
    except ValueError:
        pass

    # 3. strict уходит всегда: опечатка в поле вернётся ошибкой, а не списанием.
    assert v._body("image", "z-image", "закат", {})["strict"] is True

    # 4. Ключ идемпотентности один на все попытки одного вызова.
    seq = [(503, {"error": "rate_limit_exceeded", "retry_after": 0}), None]

    def flaky(method, url, body, headers):
        if url.endswith("/generate") and seq[0]:
            out, seq[0] = seq[0], None
            calls.append({"method": method, "url": url, "body": body})
            return out
        return fake(method, url, body, headers)

    v2 = Vibe("test-token", transport=flaky)
    calls.clear()
    v2.generate("image", "z-image", "закат")
    keys = {c["body"]["idempotency_key"] for c in calls if c["url"].endswith("/generate")}
    assert len(keys) == 1, f"ретрай ушёл с новым ключом, это двойное списание: {keys}"

    # 5. Дорогая генерация не запускается без подтверждения.
    v3 = Vibe("test-token", transport=fake, confirm_above=100)
    try:
        v3.generate("video", "grok-itv-10", "кот")
        raise AssertionError("дорогая генерация ушла без подтверждения")
    except VibeError as e:
        assert e.code == "confirm_required", e.code
    v3.generate("video", "grok-itv-10", "кот", confirm=True)   # с подтверждением проходит

    # 6. Ошибка API не теряет код: по нему вызывающий решает, что делать.
    v4 = Vibe("test-token", transport=lambda *a: (402, {"error": "insufficient_balance",
                                                       "message": "не хватает рублей"}))
    try:
        v4.generate("image", "z-image", "закат")
        raise AssertionError("402 проглочен")
    except VibeError as e:
        assert e.status == 402 and e.code == "insufficient_balance"

    # 7. Списки и одиночные значения приводятся по имени поля.
    assert _shape("image_urls", "a") == ["a"]
    assert _shape("image_url", ["a", "b"]) == "a"

    # 8. Имя модели не из каталога отбивается с подсказкой, а не уходит в 422.
    #    Это реальный случай: документация обещает grok-itv-10, каталог знает grok-itv.
    try:
        v._body("video", "grok-itv", "кот", {})
        raise AssertionError("несуществующее имя модели прошло дальше")
    except VibeError as e:
        assert e.code == "model_not_in_catalog", e.code
        assert "grok-itv-10" in e.payload["message"], e.payload["message"]

    # 9. strict нельзя снять снаружи: иначе главная защита выключается одним аргументом
    assert v._body("image", "z-image", "закат", {"strict": False})["strict"] is True

    # 10. Расширение файла берётся из имени, а не из точек в домене.
    assert _ext("https://x.io/ab", "video") == ".mp4", _ext("https://x.io/ab", "video")
    assert _ext("https://host/files/generation/7.mp4") == ".mp4"

    print("самопроверка пройдена: 10 из 10")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
