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
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Иначе на консоли без UTF-8 (обычный cmd, Git Bash) печать кириллицы падает
# с UnicodeEncodeError уже ПОСЛЕ прохождения тестов, и выглядит это как провал.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

BASE = "https://lk.vibemarketolog.ru/api/agent"

# Потолок на тело ответа: раздутый ответ от API или от ссылки на результат не
# должен съесть память процесса. 64 МБ с запасом на любое видео этого API.
_MAX_RESPONSE = 64 * 1024 * 1024


class _NoAuthLeakRedirect(urllib.request.HTTPRedirectHandler):
    """Снимает Authorization, если редирект уводит на другой хост.

    Голый urllib тащит заголовок Authorization сквозь любой редирект, и токен
    ушёл бы на чужой домен, подставленный ответом Location.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            same = urllib.parse.urlsplit(req.full_url).netloc
            if urllib.parse.urlsplit(newurl).netloc != same:
                new.headers = {k: v for k, v in new.headers.items()
                               if k.lower() != "authorization"}
        return new

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
            # свой opener без стандартного редирект-хендлера: urllib не снимает
            # заголовок Authorization при переходе на чужой хост, и токен утёк бы
            # редиректом на левый домен
            opener = urllib.request.build_opener(_NoAuthLeakRedirect())
            with opener.open(req, timeout=self.timeout) as r:
                raw = r.read(_MAX_RESPONSE + 1).decode("utf-8", "replace")
                if len(raw) > _MAX_RESPONSE:
                    return 502, {"error": "too_large",
                                 "message": f"ответ больше {_MAX_RESPONSE} байт"}
                try:
                    return r.status, json.loads(raw or "{}")
                except json.JSONDecodeError:
                    # 200 с не-JSON телом: страница прокси или WAF вместо ответа API
                    return 502, {"error": "bad_response",
                                 "message": "сервер вернул не JSON", "raw": raw[:300]}
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
        attempts = max(1, self.max_retries)
        status, payload = 0, {}
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                status, payload = self._transport(method, url, body, headers)
            except Exception as e:
                # обрыв сети, таймаут, сброс соединения: ретрай идёт с ТЕМ ЖЕ
                # ключом идемпотентности, поэтому повтор не оплачивается второй раз
                if last:
                    raise VibeError(0, {"error": "network_error",
                                        "message": f"{type(e).__name__}: {e}"}) from e
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if status < 400:
                return payload
            if status not in RETRY_STATUS or last:
                raise VibeError(status, payload)
            # сервер сам говорит, сколько ждать; свои догадки тут вредны.
            # ноль это valid ответ «повторяй сразу», поэтому сравниваем с None
            wait = payload.get("retry_after")
            time.sleep(max(0.0, float(wait)) if wait is not None else delay)
            delay = min(delay * 2, 30)
        raise VibeError(status, payload)

    # ---------- справочники ----------

    @property
    def capabilities(self):
        if self._caps is None:
            self._caps = self._call("GET", "/capabilities")
        return self._caps

    def model_spec(self, model):
        """Описание модели из живого каталога. Если имя это тир, отдаём спеку
        родителя: параметры у тира те же, отличается только цена."""
        try:
            caps = self.capabilities
        except VibeError:
            return None
        return _find_model(caps, model) or _find_tier_parent(caps, model)

    def balance(self):
        return self._call("GET", "/balance")

    def estimate(self, type, model, prompt, **fields):
        body = self._body(type, model, prompt, fields)
        return self._call("POST", "/generate/estimate", body)

    # ---------- сборка запроса ----------

    def known_models(self, type=None):
        """Имена моделей из живого каталога, ВКЛЮЧАЯ тиры.

        У части моделей каталог отдаёт поле tiers со своими ценами в
        tier_prices: grok-itv стоит 36, а его тир grok-itv-10 уже 196.
        Если считать тир опечаткой и советовать базовую модель, получится
        тихая подмена тарифа втрое дешевле запрошенного."""
        try:
            caps = self.capabilities
        except VibeError:
            return set()
        models = caps.get("models") if isinstance(caps, dict) else None
        if not isinstance(models, dict):
            return set()
        nodes = [models.get(type)] if type else list(models.values())
        out = set()
        for node in nodes:
            if not isinstance(node, dict):
                continue
            out |= set(node)
            for spec in node.values():
                if isinstance(spec, dict):
                    out |= set(spec.get("tiers") or [])
        return out

    def tier_price(self, model):
        """Цена конкретного тира, если это тир. Иначе цена самой модели."""
        spec = self.model_spec(model)
        if not isinstance(spec, dict):
            return None
        prices = spec.get("tier_prices") or {}
        return prices.get(model, spec.get("price"))

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
        prefer = fields.pop("field", None)     # явный выбор, когда полей несколько
        for kind in ("image", "video", "audio"):
            value = fields.pop(kind, None)
            if value is None:
                continue
            target = self._media_field(model, kind, prefer if kind == "image" else None)
            if not target:
                raise ValueError(
                    f"модель {model} не принимает {kind}: в её схеме нет подходящего поля. "
                    f"Проверьте GET /capabilities или уберите аргумент {kind}=")
            body[target] = _shape(target, value)
            body.update(MODE_SWITCH.get(model, {}))

        body.update(fields)
        body["strict"] = True     # после merge: иначе strict=False из kwargs тихо снимет защиту
        return {k: v for k, v in body.items() if v is not None}

    def _media_field(self, model, kind, prefer=None):
        """Ищем приёмник медиа сначала в живой схеме модели, потом в таблице.

        Если модель принимает и первый кадр, и референсные картинки, это два
        РАЗНЫХ сценария, а не синонимы: первый кадр задаёт начало ролика,
        референсы задают стиль. Молча выбирать за пользователя нельзя."""
        spec = self.model_spec(model)
        if spec is None:                       # каталог недоступен или модель новая
            return _fallback_field(model, kind)
        known = _param_names(spec)
        if prefer:
            if prefer not in known:
                raise ValueError(f"модель {model} не принимает поле {prefer}")
            return prefer
        hits = [n for n in MEDIA_FIELDS[kind] if n in known]
        if kind == "image" and len(hits) > 1:
            raise ValueError(
                f"модель {model} принимает картинку в разные поля: {', '.join(hits)}. "
                f"Это разные сценарии, выберите явно через field=, например "
                f"field='{hits[0]}'")
        return hits[0] if hits else None

    # ---------- генерация ----------

    def generate(self, type, model, prompt, confirm=False, **fields):
        """Запускает генерацию. Возвращает ответ API (для type=text он уже
        содержит текст, для остальных типов — generation_id)."""
        body = self._body(type, model, prompt, fields)

        if self.confirm_above and not confirm:
            est = self._call("POST", "/generate/estimate", body)
            raw = est.get("estimated_cost_rub", est.get("reserve_rub"))
            if raw is None:
                # цены нет, значит проверить порог нечем. Молча пропускать нельзя:
                # это ровно то дорогое списание, ради которого порог и заведён
                raise VibeError(0, {
                    "error": "estimate_unavailable",
                    "message": "смета не вернула цену, порог проверить нечем; "
                               "повторите с confirm=True, если запуск всё равно нужен",
                    "estimate": est})
            cost = float(raw)
            if cost > self.confirm_above:
                raise VibeError(0, {
                    "error": "confirm_required",
                    "message": f"смета {cost} р выше порога {self.confirm_above} р, "
                               f"повторите вызов с confirm=True",
                    "estimate": est})

        out = self._call("POST", "/generate", body, idem=str(uuid.uuid4()))

        # Сервер сообщает отброшенные поля, но по умолчанию об этом молчит в
        # логах вызывающего. Раз деньги уже списаны, случай «строгий режим не
        # сработал» обязан быть видимым, а не тихим.
        ignored = out.get("ignored_params") if isinstance(out, dict) else None
        if ignored:
            raise VibeError(0, {
                "error": "params_ignored_after_charge",
                "message": f"сервер принял запрос, списал {out.get('cost')} р и "
                           f"проигнорировал поля: {', '.join(map(str, ignored))}. "
                           f"Результат почти наверняка не тот, что вы просили",
                "generation": out})
        return out

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
        folder = os.path.abspath(folder)
        saved = []
        for i, url in enumerate(urls):
            if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
                # url приходит из ответа сервера: file:// или data: увели бы чтение
                # с диска процесса вместо скачивания результата
                raise VibeError(0, {"error": "bad_url", "url": url,
                                    "message": "ссылка на результат не http(s)"})
            # generation_id идёт из ответа сервера и попадает в имя файла: без
            # очистки '../..' в нём вывел бы запись за пределы папки
            raw_id = str(st.get("generation_id", "gen"))
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", raw_id) or "gen"
            name = f"{safe_id}{'' if len(urls) == 1 else f'_{i}'}" + _ext(url, st.get("type"))
            path = os.path.join(folder, name)
            if os.path.commonpath([folder, os.path.abspath(path)]) != folder:
                raise VibeError(0, {"error": "bad_path", "message": "имя файла выводит за папку"})
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as r:
                    blob = r.read(_MAX_RESPONSE + 1)
            except Exception as e:
                # ссылка живёт 7 дней; протухшую надо отличать от успеха явно,
                # и ошибка должна быть той же породы, что у остальных методов
                raise VibeError(0, {"error": "download_failed", "url": url,
                                    "message": f"{type(e).__name__}: {e}"}) from e
            if len(blob) > _MAX_RESPONSE:
                raise VibeError(0, {"error": "too_large", "url": url,
                                    "message": f"файл больше {_MAX_RESPONSE} байт"})
            if not blob:
                raise VibeError(0, {"error": "empty_file", "url": url,
                                    "message": "по ссылке пришёл пустой файл"})
            with open(path, "wb") as f:
                f.write(blob)
            saved.append(path)
        return saved


# ---------- разбор каталога ----------

def _find_tier_parent(caps, model):
    """Спека родителя, если имя это тир (grok-itv-10 внутри grok-itv)."""
    models = caps.get("models") if isinstance(caps, dict) else None
    if not isinstance(models, dict):
        return None
    for node in models.values():
        if not isinstance(node, dict):
            continue
        for spec in node.values():
            if isinstance(spec, dict) and model in (spec.get("tiers") or []):
                return spec
    return None


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


# Модели, которые входную картинку не принимают вовсе: это генерация из текста.
# Без этого списка запасная таблица подсовывала им поле правки, и терялись деньги
# ровно тем способом, против которого написан весь клиент.
TEXT_ONLY_IMAGE = ("z-image", "seedream-5-pro", "seedream-5-lite", "gpt-image-2",
                   "gpt-image-1.5", "grok-image")


def _fallback_field(model, kind):
    """Поле по таблице документации, когда живого каталога нет."""
    m = model.lower()
    if kind == "image":
        if m in TEXT_ONLY_IMAGE:            # точное совпадение: у -edit поле есть
            return None
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
    """Половина полей ждёт список, половина одну строку. Приводим по имени.

    Лишние значения не отбрасываем молча: клиент, который тихо теряет часть
    входа и всё равно платит за генерацию, ничем не лучше промаха полем."""
    plural = field.endswith("s")
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError(f"поле {field} получило пустой список")
        if not plural and len(value) > 1:
            raise ValueError(
                f"поле {field} принимает одно значение, а передано {len(value)}. "
                f"Уберите лишние или выберите модель, которая принимает список")
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

    # 7. Одиночное значение приводится к списку там, где поле его ждёт.
    #    Обратный случай (список в поле на одно значение) проверяется в п.15.
    assert _shape("image_urls", "a") == ["a"]
    assert _shape("image_url", "a") == "a"

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

    # 11. Обрыв сети уходит в ретрай, а не наружу сырым исключением. Иначе
    #     обещание «сетевой сбой не оплачивается дважды» ничем не обеспечено.
    import socket
    state = {"tries": 0}

    def dropped_once(method, url, body, headers):
        state["tries"] += 1
        if state["tries"] == 1:
            raise socket.timeout("сеть моргнула")
        return fake(method, url, body, headers)

    v5 = Vibe("t", transport=dropped_once)
    v5.generate("image", "z-image", "после обрыва")
    assert state["tries"] >= 2, "повтора после обрыва не было"

    # 12. retry_after=0 значит «повторяй сразу», а не «спи секунду по умолчанию».
    slept = []
    real_sleep, time.sleep = time.sleep, lambda s: slept.append(s)
    try:
        seq0 = [(503, {"error": "rate_limit_exceeded", "retry_after": 0})]

        def zero_wait(method, url, body, headers):
            if url.endswith("/generate") and seq0:
                return seq0.pop()
            return fake(method, url, body, headers)

        Vibe("t", transport=zero_wait).generate("image", "z-image", "мгновенный повтор")
    finally:
        time.sleep = real_sleep
    assert slept and slept[0] == 0, f"retry_after=0 проигнорирован, спали {slept}"

    # 13. Каталог, пришедший не словарём, не роняет клиент.
    v6 = Vibe("t", transport=lambda m, u, b, h: (200, None) if u.endswith("/capabilities")
              else (200, {}))
    assert v6.known_models("image") == set()

    # 14. Модель, которая картинку не принимает, не получает её и по запасной
    #     таблице, когда каталог недоступен. seedream-5-pro это text-to-image.
    v7 = Vibe("t", transport=lambda *a: (401, {"error": "unauthorized"}))
    assert v7._media_field("seedream-5-pro", "image") is None
    assert v7._media_field("seedream-5-pro-edit", "image") == "image_input"

    # 15. Несколько значений не влезают в поле на одно и не теряются молча.
    assert _shape("image_urls", "a") == ["a"]
    try:
        _shape("image_url", ["a", "b"])
        raise AssertionError("лишнее значение отброшено молча")
    except ValueError:
        pass

    # 16. Смета без цены не пропускает дорогую генерацию мимо порога.
    def no_price(method, url, body, headers):
        if url.endswith("/generate/estimate"):
            return 200, {"valid": True}          # цены нет
        return fake(method, url, body, headers)

    try:
        Vibe("t", transport=no_price, confirm_above=100).generate("image", "z-image", "дорого")
        raise AssertionError("генерация ушла, хотя цену проверить было нечем")
    except VibeError as e:
        assert e.code == "estimate_unavailable", e.code

    # 17. Тир это не опечатка. Каталог кладёт тиры внутрь модели, у них своя
    #     цена, и советовать вместо тира базовую модель значит подменить тариф.
    caps_t = {"models": {"video": {"grok-itv": {
        "price": 36, "tiers": ["grok-itv-10", "grok-itv-20"],
        "tier_prices": {"grok-itv-10": 196, "grok-itv-20": 316},
        "required": ["prompt", "image_urls"], "optional": ["duration"]}}}}
    v8 = Vibe("t", transport=lambda *a: (200, caps_t))
    assert "grok-itv-10" in v8.known_models("video")
    assert v8.tier_price("grok-itv-10") == 196 and v8.tier_price("grok-itv") == 36
    assert v8._body("video", "grok-itv-10", "кот", {"image": "u"})["model"] == "grok-itv-10"

    # 18. Отброшенные сервером поля не остаются незамеченными: деньги уже ушли.
    def with_ignored(method, url, body, headers):
        if url.endswith("/generate"):
            return 200, {"status": "processing", "generation_id": 5, "cost": 36,
                         "ignored_params": ["image_input"]}
        return fake(method, url, body, headers)

    try:
        Vibe("t", transport=with_ignored).generate("image", "z-image", "тест")
        raise AssertionError("списание с проигнорированными полями прошло молча")
    except VibeError as e:
        assert e.code == "params_ignored_after_charge", e.code

    # 19. Два поля под картинку это два сценария, выбор за пользователем.
    caps_m = {"models": {"video": {"seedance-2": {
        "required": ["prompt"],
        "optional": ["first_frame_url", "reference_image_urls"]}}}}
    v9 = Vibe("t", transport=lambda *a: (200, caps_m))
    try:
        v9._body("video", "seedance-2", "сцена", {"image": "u"})
        raise AssertionError("поле выбрано за пользователя молча")
    except ValueError:
        pass
    assert "reference_image_urls" in v9._body(
        "video", "seedance-2", "сцена", {"image": "u", "field": "reference_image_urls"})

    print("самопроверка пройдена: 19 из 19")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
