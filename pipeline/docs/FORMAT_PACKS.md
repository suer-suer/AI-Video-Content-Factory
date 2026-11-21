# Format Packs: подключение внешних рендереров

## Проблема

Продукт поддерживает 6 форматов (long_narrative, short_vertical, explainer,
tech_review, educational_narrative, talking_head) — все рендерятся встроенным
FFmpeg-конвейером. Этого мало: niche-форматы (мульты кодом, 3D-шорты, кинетик
типография, датавиз) требуют специфических рендереров (Remotion, Three.js,
Motion Canvas). Писать каждый своими руками — непозволительно дорого.

## Решение: Format Pack = съёмный каталог с внешним рендерером

Format pack — это папка внутри `format_packs/` с manifest-файлом и скриптами.
Продукт не реализует рендеринг сам — он **вызывает внешний скрипт** из пака,
передавая контрактные входные данные (сценарий, тайминги, субтитры, конфиг).

```
format_packs/
├── toon-stories/           # мульты кодом (Remotion, три стиля персонажа)
│   ├── pack.yaml           # метаданные: id, название, входы/выходы, команды
│   └── ...                 # всё что нужно паку (kit/, scripts/, src/)
├── kylo-3d-shorts/         # 3D-шорты (Community plugin из ZCode marketplace)
│   ├── pack.yaml
│   └── ...
└── kinetic-typography/     # пример из сообщества
    ├── pack.yaml
    └── ...
```

### pack.yaml — контракт

```yaml
id: toon-stories
name: "Toon Stories — мульты кодом"
version: "1.0"
description: "Мультяшные истории для YouTube — стикмен, кукла, 3D"
renderer: remotion            # remotion | ffmpeg | custom
entry: scripts/render.mjs    # точка входа (относительно корня пака)
formats:                      # какие форматы предоставляет пак
  - id: toon_short_916
    name: "Мульт-шорт (9:16)"
    aspect_ratio: "9:16"
    resolution: [1080, 1920]
  - id: toon_long_169
    name: "Мульт-история (16:9)"
    aspect_ratio: "16:9"
    resolution: [1920, 1080]
inputs:                       # что конвейер передаёт паку
  script: script/script.json
  subtitles: subtitles/subtitles.srt
  audio: audio/episode.wav
  world: visual/world.json
outputs:                      # что пак возвращает
  video: episode.mp4
  thumbnail: thumbnail.png
command:                      # как вызвать рендер
  stills: "node scripts/render.mjs stills <CompId> _tmp/кадры 60 300 600"
  preview: "node scripts/render.mjs preview <CompId> _tmp/превью.mp4 30"
  render: "node scripts/render.mjs render <CompId> out/<run_id>.mp4"
```

### Как это работает

1. M4 определяет формат выпуска → если формат принадлежит паку, M4 передаёт
   управление рендереру пака вместо встроенного FFmpeg.
2. Паку передаются стандартизированные входные файлы (сценарий, субтитры,
   аудио-дорожка, конфиг мира) — то, что уже производится M1–M3.
3. Пак рендерит видео своим способом (Remotion, Three.js, что угодно) и
   возвращает mp4 по пути из outputs.
4. M4 подхватывает готовый mp4, добавляет аудио-микс (если пак не сделал сам)
   и продолжает обычный флоу (M5 метаданные, M6 публикация).

### Что НЕ делает формат-пак

- Не заменяет M1–M3 (сценарий, озвучка, визуальные промпты — всегда в ядре).
- Не модифицирует контракты (JSON-схемы стадий фиксированы).
- Не имеет доступа к ключам, .env или файлам вне своего каталога.

## Где брать готовые паки (не писать самому)

| Источник | Что там есть | Как подключить |
|---|---|---|
| **Remotion Templates** (remotion.dev/templates) | 20+ шаблонов: кинетик типографика, dataviz, шаблоны Shorts, музыкальные клипы | `npx create-video@latest --template=X` в format_packs/ + pack.yaml |
| **ZCode plugins** (`/plugin marketplace add ...`) | Скиллы-генераторы видео: kylo-3d-shorts (3D), toon-stories (мульты), KineticTypography | Копировать kit/ + pack.yaml в format_packs/ |
| **Remotion Discord / community** | Пользовательские шаблоны, анимационные пресеты | То же |
| **Motion Canvas** (motioncanvas.io) | Анимации «нарисованные кодом» — alternative to Remotion | Аналогично: pack.yaml + entry point |
| **Three.js examples** (threejs.org/examples) | 3D-сцены, эффекты, шейдеры — обёртка в Remotion <ThreeCanvas> | Для 3D-паков |
| **ComfyUI workflows** | Нейрогенерация кадров/видео (Stable Video Diffusion, AnimateDiff) | Отдельный тип пака: renderer: comfyui |

## Установка пака (владелец / admin)

1. Скопировать папку пака в `pipeline/format_packs/<id>/`
2. Перечитать форматы: `POST /api/formats/reload` (или рестарт UI)
3. Новый формат появится в форме выпуска

## Статус

**Дизайн, не реализовано.** Реализация — этап P2 (открытое API расширений)
после валидации спроса (стратегия: «не строить маркетплейс до пейзеров»).
Минимальный PoC — один пак toon-stories, рендер 15-сек теста через M4-бридж.
