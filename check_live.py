"""Сверка документации с живым каталогом моделей. Ключ не нужен.

Каталог берётся открытой MCP-тулзой list_capabilities: авторизации она не
требует, значит проверка доступна кому угодно, в том числе в CI.

Зачем. Документация и каталог живут отдельно и расходятся. На 31.07.2026
документация обещает grok-itv-10 и grok-itv-20, каталог знает только grok-itv.
Модели omnihuman-1-5 и volcengine-lipsync описаны в документации подробно, с
ценой за секунду, но в каталоге отсутствуют полностью. Разработчик копирует
пример из документации и получает 422 на ровном месте.

    python check_live.py             # забрать каталог и сверить
    python check_live.py --offline   # сверить по сохранённой копии
"""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
from vibe import Vibe, _find_model, _param_names   # noqa: E402

MCP = "https://lk.vibemarketolog.ru/api/mcp"
CACHE = "_caps_live.json"

# Имена моделей, обещанные документацией на 2026-07-30 (раздел Endpoint reference
# и разборы моделей). Обновлять вместе с документацией.
DOCS = {
    "video": ["grok-ttv-10", "grok-ttv-20", "grok-itv-10", "grok-itv-20", "grok-extend",
              "grok-upscale", "veo3_fast", "veo3.1", "veo3", "kling-3.0-std", "kling-3.0-pro",
              "motion-control-720p", "motion-control-1080p", "seedance-2", "seedance-2-fast",
              "seedance-2-mini", "omnihuman-1-5", "volcengine-lipsync", "veed-avatar",
              "topview-url-video", "gemini-omni-video"],
    "image": ["z-image", "nano-banana-pro-2k", "nano-banana-2-lite", "seedream-5-pro",
              "seedream-5-pro-edit", "gpt-image-2", "gpt-image-2-edit", "gpt-image-1.5",
              "seedream-4.5", "grok-image", "gemini-omni-character"],
    "voice": ["el-tts-turbo", "el-tts-multilingual-v2", "el-dialogue-v3", "el-sound-fx-v2",
              "gemini-flash-tts", "gemini-pro-tts", "gemini-omni-audio"],
    "music": ["suno-v5", "suno-v5-instrumental", "suno-v5.5", "suno-v5.5-instrumental"],
    "text": ["claude-opus-5", "gpt-5.6-sol"],
}

# Модели, которые в документации показаны с примером входной картинки.
IMAGE_PROBES = ["grok-itv", "veo3_fast", "motion-control-720p", "seedance-2",
                "seedream-5-pro-edit", "z-image"]


def fetch():
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "list_capabilities", "arguments": {}}}
    req = urllib.request.Request(MCP, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    raw = urllib.request.urlopen(req, timeout=60).read().decode("utf-8")
    caps = json.loads(json.loads(raw)["result"]["content"][0]["text"])
    json.dump(caps, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return caps


def main():
    caps = (json.load(open(CACHE, encoding="utf-8")) if "--offline" in sys.argv else fetch())
    live = caps.get("models", {})
    print(f"каталог получен, типов {len(live)}\n")

    missing, extra_total = [], []
    print(f"{'тип':<8} {'в каталоге':>11} {'нет из доков':>13} {'сверх доков':>12}")
    for t, names in DOCS.items():
        have = set(live.get(t, {}))
        miss = [n for n in names if n not in have]
        extra = sorted(have - set(names))
        print(f"{t:<8} {len(have):>11} {len(miss):>13} {len(extra):>12}")
        missing += [(t, n) for n in miss]
        extra_total += [(t, n) for n in extra]

    if missing:
        print("\nОБЕЩАНО ДОКУМЕНТАЦИЕЙ, НО КАТАЛОГ НЕ ОТДАЁТ:")
        for t, n in missing:
            print(f"  {t:<7} {n}")
    if extra_total:
        print("\nЕСТЬ В КАТАЛОГЕ, НО НЕ ОПИСАНО В РАЗБОРАХ ДОКУМЕНТАЦИИ:")
        for t, n in extra_total:
            print(f"  {t:<7} {n}")

    # Ключ строгой проверки описан в прозе, но агент читает не прозу, а схему.
    total = sum(len(node) for node in live.values() if isinstance(node, dict))
    declared = sum(1 for node in live.values() if isinstance(node, dict)
                   for spec in node.values()
                   if "strict" in set(spec.get("required", [])) | set(spec.get("optional", [])))
    print(f"\nSTRICT В СХЕМЕ: объявлен у {declared} моделей из {total}")
    if declared == 0:
        print("  документация описывает strict отдельным разделом с примером,")
        print("  но ни одна модель не объявляет его в required/optional.")
        print("  Агент, который строит запрос по схеме, о защите не узнает.")

    partner = caps.get("partner_only")
    if partner:
        print("\nПЛАТНОЕ ВНЕ ОТКРЫТОГО API (поле partner_only каталога):")
        print(" ", str(partner)[:300])

    # проверка, что клиент правильно читает схему живого каталога
    v = Vibe("catalog-only", transport=lambda *a: (200, caps))
    v._caps = caps
    print("\nкуда клиент положит входную картинку (по живой схеме):")
    ok = 0
    for m in IMAGE_PROBES:
        spec = _find_model(caps, m)
        field = v._media_field(m, "image") if spec else None
        note = str(field) if field else ("модель картинку не принимает" if spec else "нет в каталоге")
        print(f"  {m:<22} {note}")
        if spec:
            ok += 1
    print(f"\nмоделей найдено в каталоге: {ok} из {len(IMAGE_PROBES)}")
    print(f"расхождений документации и каталога: {len(missing) + len(extra_total)}")


if __name__ == "__main__":
    main()
