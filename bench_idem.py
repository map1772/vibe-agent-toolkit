# -*- coding: utf-8 -*-
"""Проверка обещаний платформы про идемпотентность на боевом контуре.

Документация обещает две вещи: тот же ключ с другим телом даёт конфликт без
списания, а параллельный дубль с тем же телом отбивается как duplicate_request.
Если это не так, клиент платит дважды или получает чужой результат под своей
квитанцией. Проверяем на самой дешёвой модели каталога.

  python bench_idem.py --dry     показать план и цену
  python bench_idem.py --run     выполнить (списание с боевого баланса)
"""
import json, os, sys, threading, time, urllib.error, urllib.request

sys.stdout.reconfigure(encoding="utf-8")

SECRETS = os.environ.get("VIBE_SECRETS",
                         os.path.expanduser(r"~\.secrets\vibemarketolog.json"))
MODEL = "z-image"
BUDGET_RUB = 30
RACE_THREADS = 5


def cfg():
    with open(SECRETS, encoding="utf-8") as fh:
        return json.load(fh)


def call(method, path, body=None, idem=None):
    c = cfg()
    headers = {"Authorization": "Bearer " + c["api_token"], "Content-Type": "application/json"}
    if idem:
        headers["Idempotency-Key"] = idem
    req = urllib.request.Request(c["api_base"].rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:300]}


def balance():
    code, data = call("GET", "/balance")
    if isinstance(data, dict):
        for key in ("balance", "amount", "value", "rub", "current"):
            if key in data:
                return data[key]
    return None


def gen(prompt, idem, key_in_body=True):
    body = {"type": "image", "model": MODEL, "prompt": prompt}
    if key_in_body:
        body["idempotency_key"] = idem
    return call("POST", "/generate", body, idem=idem)


def main(run):
    start = balance()
    print(f"модель {MODEL}, баланс до: {start}, потолок расхода {BUDGET_RUB} р")
    if not run:
        print("--dry: боевых запросов не было")
        return

    stamp = str(int(time.time()))

    print("\n=== проба 1: тот же ключ, другое тело ===")
    key1 = "idem-body-" + stamp
    c1, r1 = gen("серый кот на подоконнике", key1)
    print(f"первый запрос:  код {c1}, cost {r1.get('cost')}, id {r1.get('generation_id')}")
    c2, r2 = gen("красная машина в горах", key1)
    print(f"второй запрос:  код {c2}, cost {r2.get('cost')}, id {r2.get('generation_id')}")
    print(f"ошибка второго: {r2.get('error') or r2.get('message') or 'её нет'}")
    same_id = r1.get("generation_id") and r1.get("generation_id") == r2.get("generation_id")
    print("тот же generation_id на другое тело:", "ДА, подмена" if same_id else "нет")

    mid = balance()
    print("баланс после пробы 1:", mid)
    if start is not None and mid is not None and start - mid > BUDGET_RUB:
        sys.exit("потолок расхода исчерпан, вторую пробу не запускаю")

    print(f"\n=== проба 2: {RACE_THREADS} параллельных запросов с одним ключом и телом ===")
    key2 = "idem-race-" + stamp
    out = []
    lock = threading.Lock()

    def shot(i):
        code, data = gen("синий шар на белом фоне", key2)
        with lock:
            out.append((i, code, data.get("cost"), data.get("generation_id"),
                        data.get("error") or data.get("message")))

    threads = [threading.Thread(target=shot, args=(i,)) for i in range(RACE_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i, code, cost, gid, err in sorted(out):
        print(f"  поток {i}: код {code}, cost {cost}, id {gid}, ошибка {err}")
    charged = [row for row in out if row[2]]
    print(f"списаний в ответах: {len(charged)} из {RACE_THREADS}")

    time.sleep(5)
    end = balance()
    print("\nбаланс после всего:", end)
    if start is not None and end is not None:
        print(f"итого потрачено: {round(start - end, 2)} р")


if __name__ == "__main__":
    if "--run" not in sys.argv and "--dry" not in sys.argv:
        sys.exit(__doc__)
    main("--run" in sys.argv)
