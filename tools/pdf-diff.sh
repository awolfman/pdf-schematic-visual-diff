#!/usr/bin/env bash
set -euo pipefail

# Разрешение сравнения и растровых страниц результата.
DPI="${PDF_DIFF_DPI:-300}"
OUTPUT_DPI="${PDF_DIFF_OUTPUT_DPI:-300}"

# Параллелизм сравнения и постраничной растеризации.
JOBS="${PDF_DIFF_JOBS:-3}"
RENDER_JOBS="${PDF_DIFF_RENDER_JOBS:-4}"

# Параметры выделения.
DIFF_THRESHOLD="${PDF_DIFF_THRESHOLD:-1%}"
DILATE_KERNEL="${PDF_DIFF_DILATE:-Disk:1}"
PNG_LEVEL="${PDF_DIFF_PNG_LEVEL:-9}"

export MAGICK_THREAD_LIMIT=1
export OMP_NUM_THREADS=1

log() {
    printf '[PDF diff] %s\n' "$*"
}

die() {
    printf '[PDF diff] Ошибка: %s\n' "$*" >&2
    exit 1
}

for tool in \
    bash git magick pdftocairo pdfinfo img2pdf qpdf \
    parallel python3 cmp mktemp sleep
do
    command -v "$tool" >/dev/null 2>&1 ||
        die "Не установлена утилита $tool"
done

for value in "$DPI" "$OUTPUT_DPI" "$JOBS" "$RENDER_JOBS"; do
    [[ "$value" =~ ^[1-9][0-9]*$ ]] ||
        die "DPI и количество заданий должны быть положительными целыми числами."
done

(( OUTPUT_DPI <= DPI )) ||
    die "Выходной DPI не должен превышать DPI сравнения."

[[ "$PNG_LEVEL" =~ ^[0-9]$ ]] ||
    die "PDF_DIFF_PNG_LEVEL должен быть целым числом от 0 до 9."

export PARALLEL_SHELL
PARALLEL_SHELL="$(command -v bash)"

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

export GIT_LITERAL_PATHSPECS=1

HAS_HEAD=0
if git rev-parse --verify HEAD >/dev/null 2>&1; then
    HAS_HEAD=1
fi

TMP_PARENT="${PDF_DIFF_TMPDIR:-${TMPDIR:-/tmp}}"
[[ -d "$TMP_PARENT" ]] ||
    die "Временный каталог не существует: $TMP_PARENT"

TMP_ROOT="$(mktemp -d "$TMP_PARENT/pdf-diff.XXXXXXXX")"
TMP_ROOT="$(cd "$TMP_ROOT" && pwd -P)"

HEARTBEAT_PID=""
ACTIVE_PARALLEL_PID=""

start_heartbeat() {
    local message="$1"

    (
        sleeper=""

        stop_worker() {
            trap - TERM INT

            if [[ -n "$sleeper" ]]; then
                kill "$sleeper" 2>/dev/null || true
                wait "$sleeper" 2>/dev/null || true
            fi

            exit 0
        }

        trap stop_worker TERM INT

        while true; do
            sleep 15 &
            sleeper=$!

            wait "$sleeper" || exit 0
            sleeper=""

            printf '[PDF diff] %s\n' "$message" >&2
        done
    ) &

    HEARTBEAT_PID=$!
}

stop_heartbeat() {
    if [[ -n "$HEARTBEAT_PID" ]]; then
        kill "$HEARTBEAT_PID" 2>/dev/null || true
        wait "$HEARTBEAT_PID" 2>/dev/null || true
        HEARTBEAT_PID=""
    fi
}

cleanup() {
    local status=$?

    trap - EXIT INT TERM
    stop_heartbeat

    if [[ -n "$ACTIVE_PARALLEL_PID" ]]; then
        # GNU Parallel прекращает запуск новых заданий.
        # Дожидаемся уже работающих перед удалением файлов.
        kill -TERM "$ACTIVE_PARALLEL_PID" 2>/dev/null || true
        wait "$ACTIVE_PARALLEL_PID" 2>/dev/null || true
        ACTIVE_PARALLEL_PID=""
    fi

    rm -rf -- "$TMP_ROOT"

    if [[ "$status" -ne 0 ]]; then
        printf '[PDF diff] Работа прервана. Проверьте сообщения выше.\n' >&2
    fi

    exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_parallel() {
    local jobs="$1"
    local argument_count="$2"
    local worker="$3"
    local task_file="$4"
    local status=0

    parallel \
        --will-cite \
        --line-buffer \
        --jobs "$jobs" \
        --null -N "$argument_count" \
        --halt soon,fail=1 \
        "$worker" < "$task_file" &

    ACTIVE_PARALLEL_PID=$!

    if wait "$ACTIVE_PARALLEL_PID"; then
        status=0
    else
        status=$?
    fi

    ACTIVE_PARALLEL_PID=""
    return "$status"
}

mkdir -p "$TMP_ROOT/magick-cache"
export MAGICK_TEMPORARY_PATH="$TMP_ROOT/magick-cache"

git diff --cached --no-renames --name-only \
    --diff-filter=D -z > "$TMP_ROOT/deleted.list"

git diff --cached --no-renames --name-only \
    --diff-filter=ACM -z > "$TMP_ROOT/staged.list"

while IFS= read -r -d '' DEL_FILE; do
    case "$DEL_FILE" in
        *_diff.pdf) continue ;;
        *.pdf) ;;
        *) continue ;;
    esac

    DIFF_FILE="${DEL_FILE%.pdf}_diff.pdf"

    if git ls-files --error-unmatch -- "$DIFF_FILE" \
        >/dev/null 2>&1; then
        log "Удаление diff для удалённого документа: $DIFF_FILE"
        git rm -f -- "$DIFF_FILE"
    elif [[ -f "$DIFF_FILE" ]]; then
        log "Удаление diff для удалённого документа: $DIFF_FILE"
        rm -f -- "$DIFF_FILE"
    fi
done < "$TMP_ROOT/deleted.list"

STAGED_PDFS=()

while IFS= read -r -d '' PDF_FILE; do
    case "$PDF_FILE" in
        *_diff.pdf) continue ;;
        *.pdf) STAGED_PDFS+=("$PDF_FILE") ;;
    esac
done < "$TMP_ROOT/staged.list"

if [[ ${#STAGED_PDFS[@]} -eq 0 ]]; then
    exit 0
fi

# Небольшой помощник проверяет геометрию без загрузки
# крупных растров в ImageMagick.
HELPER="$TMP_ROOT/check_geometry.py"

cat > "$HELPER" <<'PY'
import math
import re
import struct
import sys
from pathlib import Path


def fail(message):
    raise SystemExit("[PDF diff] Ошибка: " + message)


def parse_limit(text):
    text = text.strip()

    if text.lower() in {"unlimited", "infinity", "infinite"}:
        return 0

    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?)(?:P)?",
        text,
        re.IGNORECASE,
    )

    if not match:
        fail("Не удалось разобрать лимит ImageMagick: " + text)

    suffix = match.group(2).upper()
    multiplier = {
        "": 1,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
        "P": 1000**5,
        "E": 1000**6,
    }[suffix]

    # В записи 32000P последний P означает пиксели.
    if text.upper().endswith("P") and suffix == "P":
        multiplier = 1

    result = int(float(match.group(1)) * multiplier)

    if result <= 0:
        fail("Некорректный лимит размера: " + text)

    return result


def check_dimensions(width, height, limit_w, limit_h, label):
    if width <= 0 or height <= 0:
        fail("Некорректный размер изображения: " + label)

    if (limit_w and width > limit_w) or (
        limit_h and height > limit_h
    ):
        fail(
            f"{label}: размер {width} x {height} пикселей "
            f"превышает лимиты ширины/высоты "
            f"{limit_w or 'без ограничения'} / "
            f"{limit_h or 'без ограничения'}. "
            "Уменьшите PDF_DIFF_DPI или измените системную "
            "политику ImageMagick."
        )


def png_size(path):
    with open(path, "rb") as stream:
        header = stream.read(24)

    if (
        len(header) != 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
    ):
        fail("Некорректный заголовок PNG: " + path)

    return struct.unpack(">II", header[16:24])


mode = sys.argv[1]

if mode == "limits":
    text = Path(sys.argv[2]).read_text()

    values = []
    for name in ("Width", "Height"):
        match = re.search(
            rf"^\s*{name}:\s*(.+?)\s*$",
            text,
            re.MULTILINE | re.IGNORECASE,
        )
        if not match:
            fail("Не найден лимит " + name)

        values.append(parse_limit(match.group(1)))

    print(*values)

elif mode == "preflight":
    info_file = sys.argv[2]
    dpi = int(sys.argv[3])
    expected = int(sys.argv[4])
    limit_w = int(sys.argv[5])
    limit_h = int(sys.argv[6])
    label = sys.argv[7]

    text = Path(info_file).read_text()
    sizes = {}
    rotations = {}

    number = r"([0-9]+(?:\.[0-9]+)?)"

    for line in text.splitlines():
        match = re.match(
            rf"^Page(?:\s+([0-9]+))?\s+size:\s*"
            rf"{number}\s+x\s+{number}\s+pts",
            line,
        )
        if match:
            page = int(match.group(1) or 1)
            sizes[page] = (
                float(match.group(2)),
                float(match.group(3)),
            )
            continue

        match = re.match(
            r"^Page(?:\s+([0-9]+))?\s+rot:\s*(-?[0-9]+)",
            line,
        )
        if match:
            rotations[int(match.group(1) or 1)] = int(match.group(2))

    for page in range(1, expected + 1):
        if page not in sizes:
            fail(
                f"Не удалось прочитать размер страницы {page} "
                f"через pdfinfo для {label}."
            )

        width_pt, height_pt = sizes[page]

        if rotations.get(page, 0) % 180 == 90:
            width_pt, height_pt = height_pt, width_pt

        # Небольшой запас из-за округления вывода pdfinfo.
        width = math.ceil(width_pt * dpi / 72) + 2
        height = math.ceil(height_pt * dpi / 72) + 2

        check_dimensions(
            width, height, limit_w, limit_h,
            f"{label}, страница {page}",
        )

elif mode == "png":
    limit_w = int(sys.argv[2])
    limit_h = int(sys.argv[3])
    paths = [path for path in sys.argv[4:] if path]

    if not paths:
        fail("Не переданы PNG для проверки.")

    width = 0
    height = 0

    for path in paths:
        current_w, current_h = png_size(path)
        check_dimensions(
            current_w, current_h, limit_w, limit_h, path
        )
        width = max(width, current_w)
        height = max(height, current_h)

    check_dimensions(
        width, height, limit_w, limit_h,
        "Общий холст сравнения",
    )
    print(width, height)

else:
    fail("Неизвестный режим проверки геометрии.")
PY

log "Проверка действующих ограничений ImageMagick."

LC_ALL=C magick identify -list resource > "$TMP_ROOT/resources.txt"

LIMIT_VALUES="$(
    python3 "$HELPER" limits "$TMP_ROOT/resources.txt"
)"

read -r WIDTH_LIMIT HEIGHT_LIMIT <<< "$LIMIT_VALUES"

RESIZE_PERCENT="$(
    python3 - "$DPI" "$OUTPUT_DPI" <<'PY'
import sys
source, target = map(int, sys.argv[1:])
print(f"{100 * target / source:.10f}")
PY
)"

export PDF_DIFF_HELPER="$HELPER"
export PDF_DIFF_WIDTH_LIMIT="$WIDTH_LIMIT"
export PDF_DIFF_HEIGHT_LIMIT="$HEIGHT_LIMIT"
export PDF_DIFF_THRESHOLD="$DIFF_THRESHOLD"
export PDF_DIFF_DILATE="$DILATE_KERNEL"
export PDF_DIFF_PNG_LEVEL="$PNG_LEVEL"
export PDF_DIFF_INPUT_DPI="$DPI"
export PDF_DIFF_OUTPUT_DPI="$OUTPUT_DPI"
export PDF_DIFF_RESIZE_PERCENT="$RESIZE_PERCENT"

get_pdf_content() {
    local rev_spec="$1"
    local file_path="$2"
    local out_file="$3"
    local first_line=""

    if ! git cat-file --filters --path="$file_path" \
        "$rev_spec" > "$out_file"; then
        git show "$rev_spec" > "$out_file"
    fi

    IFS= read -r first_line < "$out_file" || true

    if [[ "$first_line" == \
        "version https://git-lfs.github.com/spec/v1"* ]]; then

        if ! git lfs version >/dev/null 2>&1; then
            printf '[PDF diff] Ошибка: требуется Git LFS для %s\n' \
                "$file_path" >&2
            return 1
        fi

        mv -- "$out_file" "${out_file}.pointer"

        GIT_LFS_SKIP_SMUDGE=0 git lfs smudge -- "$file_path" \
            < "${out_file}.pointer" > "$out_file"

        rm -f -- "${out_file}.pointer"

        first_line=""
        IFS= read -r first_line < "$out_file" || true

        if [[ "$first_line" == \
            "version https://git-lfs.github.com/spec/v1"* ]]; then
            printf '[PDF diff] Ошибка: Git LFS не вернул PDF для %s\n' \
                "$file_path" >&2
            return 1
        fi
    fi
}

check_pdf_geometry() {
    local input="$1"
    local count="$2"
    local label="$3"
    local info_file="$4"

    LC_ALL=C pdfinfo \
        -f 1 -l "$count" -box \
        "$input" > "$info_file"

    python3 "$PDF_DIFF_HELPER" preflight \
        "$info_file" "$DPI" "$count" \
        "$PDF_DIFF_WIDTH_LIMIT" "$PDF_DIFF_HEIGHT_LIMIT" \
        "$label"
}

render_single_page() {
    set -euo pipefail

    local pdf_file="$1"
    local out_dir="$2"
    local page_num="$3"
    local dpi="$4"
    local label="$5"
    local total="$6"
    local page_index
    local output

    printf -v page_index '%06d' "$page_num"
    output="$out_dir/p-${page_index}.png"

    pdftocairo \
        -png \
        -singlefile \
        -r "$dpi" \
        -f "$page_num" \
        -l "$page_num" \
        "$pdf_file" \
        "$out_dir/p-${page_index}"

    [[ -s "$output" ]] || {
        printf '[PDF diff] Ошибка: не получена страница %s.\n' \
            "$page_num" >&2
        return 1
    }

    # Проверяем реальные размеры без чтения PNG через ImageMagick.
    python3 "$PDF_DIFF_HELPER" png \
        "$PDF_DIFF_WIDTH_LIMIT" "$PDF_DIFF_HEIGHT_LIMIT" \
        "$output" >/dev/null

    printf '[PDF diff] Растеризация: %s, страница %s/%s готова.\n' \
        "$label" "$page_num" "$total"
}

process_page() {
    set -euo pipefail

    local OLD_PAGE="$1"
    local NEW_PAGE="$2"
    local OUT_PAGE="$3"
    local TMP_DIR="$4"
    local PAGE_NUMBER="$5"

    local THRESHOLD="$PDF_DIFF_THRESHOLD"
    local KERNEL="$PDF_DIFF_DILATE"
    local COMPRESSION="$PDF_DIFF_PNG_LEVEL"

    if [[ -z "$OLD_PAGE" && -z "$NEW_PAGE" ]]; then
        printf '[PDF diff] Ошибка: отсутствуют обе версии страницы %s.\n' \
            "$PAGE_NUMBER" >&2
        return 1
    fi

    if [[ -n "$OLD_PAGE" && -n "$NEW_PAGE" ]] &&
        cmp -s -- "$OLD_PAGE" "$NEW_PAGE"; then

        printf '%s\n' original > "${OUT_PAGE}.original"
        rm -f -- "$OLD_PAGE" "$NEW_PAGE"

        printf '[PDF diff] Страница %s/%s готова, без изменений.\n' \
            "$PAGE_NUMBER" "$PDF_DIFF_TOTAL_PAGES"
        return 0
    fi

    printf '[PDF diff] Страница %s/%s: расчёт масок изменений.\n' \
        "$PAGE_NUMBER" "$PDF_DIFF_TOTAL_PAGES"

    local geometry WIDTH HEIGHT SIZE

    geometry="$(
        python3 "$PDF_DIFF_HELPER" png \
            "$PDF_DIFF_WIDTH_LIMIT" "$PDF_DIFF_HEIGHT_LIMIT" \
            "$OLD_PAGE" "$NEW_PAGE"
    )"

    read -r WIDTH HEIGHT <<< "$geometry"
    SIZE="${WIDTH}x${HEIGHT}"

    local PAGE_DIR="$TMP_DIR/page_${PAGE_NUMBER}"
    mkdir -p "$PAGE_DIR"

    local OLD_GRAY="$PAGE_DIR/old_gray.miff"
    local NEW_GRAY="$PAGE_DIR/new_gray.miff"
    local ADDED_MASK="$PAGE_DIR/added_mask.png"
    local REMOVED_MASK="$PAGE_DIR/removed_mask.png"

    # Только две серые версии вместо дополнительных RGB-файлов.
    if [[ -n "$OLD_PAGE" ]]; then
        magick "$OLD_PAGE" \
            -background white -alpha remove -alpha off \
            -colorspace Gray \
            -gravity northwest -extent "$SIZE" \
            -depth 8 -compress None "$OLD_GRAY"
    else
        magick -size "$SIZE" xc:white \
            -colorspace Gray -alpha off \
            -depth 8 -compress None "$OLD_GRAY"
    fi

    if [[ -n "$NEW_PAGE" ]]; then
        magick "$NEW_PAGE" \
            -background white -alpha remove -alpha off \
            -colorspace Gray \
            -gravity northwest -extent "$SIZE" \
            -depth 8 -compress None "$NEW_GRAY"
    else
        magick -size "$SIZE" xc:white \
            -colorspace Gray -alpha off \
            -depth 8 -compress None "$NEW_GRAY"
    fi

    # Маски бинарные, поэтому храним их как компактные PNG.
    magick "$OLD_GRAY" "$NEW_GRAY" \
        -alpha off \
        -define compose:clamp=true \
        -compose MinusSrc -composite \
        -threshold "$THRESHOLD" \
        -morphology Dilate "$KERNEL" \
        -depth 8 \
        -define png:compression-level=1 \
        "$ADDED_MASK"

    magick "$NEW_GRAY" "$OLD_GRAY" \
        -alpha off \
        -define compose:clamp=true \
        -compose MinusSrc -composite \
        -threshold "$THRESHOLD" \
        -morphology Dilate "$KERNEL" \
        -depth 8 \
        -define png:compression-level=1 \
        "$REMOVED_MASK"

    local MASK_MAXIMA

    # Это получение статистики, а не попиксельное выражение -fx.
    MASK_MAXIMA="$(
        magick "$ADDED_MASK" "$REMOVED_MASK" \
            -format '%[fx:maxima]' info:
    )"

    if [[ "$MASK_MAXIMA" == "00" && -n "$NEW_PAGE" ]]; then
        printf '%s\n' original > "${OUT_PAGE}.original"

        rm -rf -- "$PAGE_DIR"

        if [[ -n "$OLD_PAGE" ]]; then
            rm -f -- "$OLD_PAGE"
        fi
        rm -f -- "$NEW_PAGE"

        printf '[PDF diff] Страница %s/%s: отличий выше порога %s нет, перенос исходной страницы.\n' \
            "$PAGE_NUMBER" "$PDF_DIFF_TOTAL_PAGES" "$THRESHOLD"

        return 0
    fi

    local BASE_SOURCE="$NEW_GRAY"
    if [[ -z "$NEW_PAGE" ]]; then
        BASE_SOURCE="$OLD_GRAY"
    fi

    local -a RESIZE_ARGS=()

    if (( PDF_DIFF_OUTPUT_DPI < PDF_DIFF_INPUT_DPI )); then
        RESIZE_ARGS=(
            -filter Lanczos
            -resize "${PDF_DIFF_RESIZE_PERCENT}%"
        )
    fi

    # Подложка и цветные слои создаются внутри одной команды.
    # После каждого Over временный слой поглощается композицией.
    magick "$BASE_SOURCE" \
        -colorspace sRGB -type TrueColor \
        -fill white -colorize 55% \
        \( \
            -size "$SIZE" xc:red \
            -colorspace sRGB -type TrueColor \
            \( "$ADDED_MASK" -alpha off \) \
            -compose CopyOpacity -composite \
        \) \
        -compose Over -composite \
        \( \
            -size "$SIZE" xc:blue \
            -colorspace sRGB -type TrueColor \
            \( "$REMOVED_MASK" -alpha off \) \
            -compose CopyOpacity -composite \
        \) \
        -compose Over -composite \
        -background white -alpha remove -alpha off \
        "${RESIZE_ARGS[@]}" \
        -type TrueColor -depth 8 \
        -define png:compression-level="$COMPRESSION" \
        "PNG24:$OUT_PAGE"

    rm -rf -- "$PAGE_DIR"

    if [[ -n "$OLD_PAGE" ]]; then
        rm -f -- "$OLD_PAGE"
    fi

    if [[ -n "$NEW_PAGE" ]]; then
        rm -f -- "$NEW_PAGE"
    fi

    printf '[PDF diff] Страница %s/%s готова, результат сравнения сохранён.\n' \
        "$PAGE_NUMBER" "$PDF_DIFF_TOTAL_PAGES"
}

export -f render_single_page process_page

FILE_INDEX=0
FILE_COUNT=${#STAGED_PDFS[@]}

for PDF_FILE in "${STAGED_PDFS[@]}"; do
    FILE_INDEX=$((FILE_INDEX + 1))

    DIFF_PDF="${PDF_FILE%.pdf}_diff.pdf"
    WORK_DIR="$TMP_ROOT/work_${FILE_INDEX}"

    mkdir -p \
        "$WORK_DIR/old_pages" \
        "$WORK_DIR/new_pages" \
        "$WORK_DIR/diff_pages" \
        "$WORK_DIR/tmp_masks"

    log "Документ $FILE_INDEX/$FILE_COUNT: $PDF_FILE"
    log "Получение версий PDF из HEAD и индекса Git."

    start_heartbeat "Получение PDF из Git продолжается."

    OLD_EXISTS=0

    if [[ "$HAS_HEAD" -eq 1 ]] &&
        git cat-file -e "HEAD:$PDF_FILE" 2>/dev/null; then

        get_pdf_content \
            "HEAD:$PDF_FILE" "$PDF_FILE" "$WORK_DIR/old.pdf"
        OLD_EXISTS=1
    fi

    get_pdf_content \
        ":$PDF_FILE" "$PDF_FILE" "$WORK_DIR/new.pdf"

    stop_heartbeat

    log "Проверка количества и размеров страниц."
    start_heartbeat "Проверка документов продолжается."

    OLD_TOTAL_PAGES=0

    if [[ "$OLD_EXISTS" -eq 1 ]]; then
        OLD_TOTAL_PAGES="$(qpdf --show-npages "$WORK_DIR/old.pdf")"

        [[ "$OLD_TOTAL_PAGES" =~ ^[1-9][0-9]*$ ]] ||
            die "Не удалось определить количество страниц старого PDF."

        check_pdf_geometry \
            "$WORK_DIR/old.pdf" "$OLD_TOTAL_PAGES" \
            "старая версия" "$WORK_DIR/old.info"
    fi

    NEW_TOTAL_PAGES="$(qpdf --show-npages "$WORK_DIR/new.pdf")"

    [[ "$NEW_TOTAL_PAGES" =~ ^[1-9][0-9]*$ ]] ||
        die "Не удалось определить количество страниц нового PDF."

    check_pdf_geometry \
        "$WORK_DIR/new.pdf" "$NEW_TOTAL_PAGES" \
        "новая версия" "$WORK_DIR/new.info"

    stop_heartbeat

    MAX_PAGES=$(( OLD_TOTAL_PAGES > NEW_TOTAL_PAGES \
        ? OLD_TOTAL_PAGES : NEW_TOTAL_PAGES ))

    # Оба набора заданий направляются в один файл.
    {
        for ((p=1; p<=MAX_PAGES; p++)); do
            if (( p <= OLD_TOTAL_PAGES )); then
                printf '%s\0%s\0%s\0%s\0%s\0%s\0' \
                    "$WORK_DIR/old.pdf" \
                    "$WORK_DIR/old_pages" \
                    "$p" "$DPI" \
                    "старая версия" "$OLD_TOTAL_PAGES"
            fi

            if (( p <= NEW_TOTAL_PAGES )); then
                printf '%s\0%s\0%s\0%s\0%s\0%s\0' \
                    "$WORK_DIR/new.pdf" \
                    "$WORK_DIR/new_pages" \
                    "$p" "$DPI" \
                    "новая версия" "$NEW_TOTAL_PAGES"
            fi
        done
    } > "$WORK_DIR/render.tasks"

    log "Растеризация при $DPI DPI. Одновременных заданий $RENDER_JOBS."
    start_heartbeat "Растеризация страниц продолжается."

    run_parallel \
        "$RENDER_JOBS" 6 render_single_page \
        "$WORK_DIR/render.tasks"

    stop_heartbeat

    for ((p=1; p<=MAX_PAGES; p++)); do
        printf -v PAD_INDEX '%06d' "$p"

        if (( p <= OLD_TOTAL_PAGES )); then
            [[ -s "$WORK_DIR/old_pages/p-${PAD_INDEX}.png" ]] ||
                die "Не получена старая страница $p."
        fi

        if (( p <= NEW_TOTAL_PAGES )); then
            [[ -s "$WORK_DIR/new_pages/p-${PAD_INDEX}.png" ]] ||
                die "Не получена новая страница $p."
        fi
    done

    export PDF_DIFF_TOTAL_PAGES="$MAX_PAGES"

    {
        for ((p=1; p<=MAX_PAGES; p++)); do
            printf -v PAD_INDEX '%06d' "$p"

            OLD_PAGE=""
            NEW_PAGE=""

            if (( p <= OLD_TOTAL_PAGES )); then
                OLD_PAGE="$WORK_DIR/old_pages/p-${PAD_INDEX}.png"
            fi

            if (( p <= NEW_TOTAL_PAGES )); then
                NEW_PAGE="$WORK_DIR/new_pages/p-${PAD_INDEX}.png"
            fi

            OUT_PAGE="$WORK_DIR/diff_pages/diff-${PAD_INDEX}.png"

            printf '%s\0%s\0%s\0%s\0%s\0' \
                "$OLD_PAGE" \
                "$NEW_PAGE" \
                "$OUT_PAGE" \
                "$WORK_DIR/tmp_masks" \
                "$p"
        done
    } > "$WORK_DIR/compare.tasks"

    log "Сравнение страниц. Было $OLD_TOTAL_PAGES, стало $NEW_TOTAL_PAGES."
    start_heartbeat "Обработка страниц продолжается."

    run_parallel \
        "$JOBS" 5 process_page \
        "$WORK_DIR/compare.tasks"

    stop_heartbeat

    CHANGED_IMAGES=()
    ORIGINAL_COUNT=0

    for ((p=1; p<=MAX_PAGES; p++)); do
        printf -v PAD_INDEX '%06d' "$p"
        OUT_PAGE="$WORK_DIR/diff_pages/diff-${PAD_INDEX}.png"

        if [[ -f "${OUT_PAGE}.original" ]]; then
            (( p <= NEW_TOTAL_PAGES )) ||
                die "Нет исходной страницы $p."

            [[ ! -f "$OUT_PAGE" ]] ||
                die "Неоднозначный результат для страницы $p."

            ORIGINAL_COUNT=$((ORIGINAL_COUNT + 1))
        elif [[ -s "$OUT_PAGE" ]]; then
            CHANGED_IMAGES+=("$OUT_PAGE")
        else
            die "Отсутствует результат для страницы $p."
        fi
    done

    CHANGED_COUNT=${#CHANGED_IMAGES[@]}

    log "Сборка PDF. Исходных страниц $ORIGINAL_COUNT, страниц diff $CHANGED_COUNT."
    start_heartbeat "Сборка итогового PDF продолжается."

    if [[ "$CHANGED_COUNT" -gt 0 ]]; then
        img2pdf \
            --imgsize "${OUTPUT_DPI}dpi" \
            -o "$WORK_DIR/changed.pdf" \
            "${CHANGED_IMAGES[@]}"
    fi

    QPDF_PAGES=()
    CHANGED_INDEX=0

    for ((p=1; p<=MAX_PAGES; p++)); do
        printf -v PAD_INDEX '%06d' "$p"
        OUT_PAGE="$WORK_DIR/diff_pages/diff-${PAD_INDEX}.png"

        if [[ -f "${OUT_PAGE}.original" ]]; then
            QPDF_PAGES+=("$WORK_DIR/new.pdf" "$p")
        else
            CHANGED_INDEX=$((CHANGED_INDEX + 1))
            QPDF_PAGES+=("$WORK_DIR/changed.pdf" "$CHANGED_INDEX")
        fi
    done

    # Отдельный обзорный документ без переноса всех
    # документных закладок и метаданных.
    qpdf \
        --empty \
        --object-streams=generate \
        --pages "${QPDF_PAGES[@]}" -- \
        "$WORK_DIR/result.pdf"

    RESULT_COUNT="$(qpdf --show-npages "$WORK_DIR/result.pdf")"

    [[ "$RESULT_COUNT" -eq "$MAX_PAGES" ]] ||
        die "Количество страниц собранного PDF неверно."

    stop_heartbeat

    log "Сохранение $DIFF_PDF и добавление в индекс Git."
    start_heartbeat "Сохранение результата и добавление в Git продолжается."

    mv -f -- "$WORK_DIR/result.pdf" "$DIFF_PDF"
    git add -- "$DIFF_PDF"

    stop_heartbeat

    rm -rf -- "$WORK_DIR"

    log "Готово: $DIFF_PDF"
done
