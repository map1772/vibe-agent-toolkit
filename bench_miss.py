# -*- coding: utf-8 -*-
"""Замер цены промаха именем поля на боевом контуре Agent API.

Вопрос, на который отвечает замер: если агент кладёт картинку в поле с почти
верным именем, сервер отбивает запрос или молча считает генерацию без картинки
и списывает деньги. Ответ нужен цифрой, снятой руками, а не из прайса.

  python bench_miss.py --dry     показать план и цену, ничего не отправлять
  python bench_miss.py --run     выполнить замер (списание с боевого баланса)
"""
import json, os, sys, time, urllib.error, urllib.request

sys.stdout.reconfigure(encoding="utf-8")

# путь к учётке из окружения, чтобы имя пользователя машины не торчало в
# публичном репозитории; дефолт для нашей машины оставлен как запасной
SECRETS = os.environ.get("VIBE_SECRETS",
                         os.path.expanduser(r"~\.secrets\vibemarketolog.json"))
MODEL = "grok-itv"
RIGHT_FIELD = "image_urls"
WRONG_FIELD = "image_url"
# их проверка ссылки ходит методом HEAD, а многие хосты отдают на него 405 и
# картинка помечается недоступной, хотя открывается. Берём хост, где HEAD отвечает 200
PICTURE = "https://triumph-digital.ru/og-image.png"
PROMPT = "плавный проезд камеры вдоль стола с ноутбуком, мягкий свет"
CEILING_RUB = 50


def cfg():
    with open(SECRETS, encoding="utf-8") as fh:
        return json.load(fh)


def call(method, path, body=None):
    c = cfg()
    req = urllib.request.Request(
        c["api_base"].rstrip("/") + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": "Bearer " + c["api_token"],
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:500]}


def balance():
    code, data = call("GET", "/balance")
    for key in ("balance", "amount", "value", "rub"):
        if isinstance(data, dict) and key in data:
            return data[key]
    return data if code == 200 else f"ошибка {code}: {data}"


def estimate(field):
    return call("POST", "/generate/estimate",
                {"type": "video", "model": MODEL, "prompt": PROMPT, field: [PICTURE]})


def main(run):
    print("модель:", MODEL, "| правильное поле:", RIGHT_FIELD, "| промах:", WRONG_FIELD)
    print("баланс до:", balance())

    code, est = estimate(RIGHT_FIELD)
    print(f"\nсмета с верным полем: код {code}, ответ {json.dumps(est, ensure_ascii=False)[:300]}")

    code_w, est_w = estimate(WRONG_FIELD)
    print(f"смета с промахом:     код {code_w}, ответ {json.dumps(est_w, ensure_ascii=False)[:300]}")

    price = None
    for key in ("price", "cost", "total", "amount"):
        if isinstance(est, dict) and key in est:
            price = est[key]
            break
    print("\nцена по смете:", price)
    if isinstance(price, (int, float)) and price > CEILING_RUB:
        sys.exit(f"цена {price} выше потолка {CEILING_RUB} рублей, замер не запускаю")

    if not run:
        print("\n--dry: боевой запрос не отправлялся")
        return

    body = {"type": "video", "model": MODEL, "prompt": PROMPT, WRONG_FIELD: [PICTURE]}
    code, out = call("POST", "/generate", body)
    print(f"\nбоевой запрос с промахом: код {code}")
    print(json.dumps(out, ensure_ascii=False)[:800])
    print("\nignored_params:", (out or {}).get("ignored_params", "поля нет в ответе"))

    time.sleep(5)
    print("баланс после:", balance())


if __name__ == "__main__":
    if "--run" not in sys.argv and "--dry" not in sys.argv:
        sys.exit(__doc__)
    main("--run" in sys.argv)
