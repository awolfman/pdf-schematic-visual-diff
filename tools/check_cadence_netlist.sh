#!/usr/bin/env bash
set -u
set -o pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    printf '[Cadence] Ошибка: каталог не является Git-репозиторием.\n' >&2
    exit 2
}

cd "$REPO_ROOT" || {
    printf '[Cadence] Ошибка: не удалось перейти в корень репозитория.\n' >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

CHECK_EXPORT="$SCRIPT_DIR/check_capture_export.py"
PARSE_NETLIST="$SCRIPT_DIR/parse_capture_netlist.py"
COMPARE_NETLISTS="$SCRIPT_DIR/compare_netlists.py"

EXPORT_DIR="${CADENCE_EXPORT_DIR:-$REPO_ROOT/.capture-export}"
BASELINE_RELATIVE="${CADENCE_BASELINE_JSON:-netlists/cadence.json}"
BASELINE_FILE="$REPO_ROOT/$BASELINE_RELATIVE"

PYTHON_BIN="${PYTHON_BIN:-python3}"

TMP_PARENT="${TMPDIR:-/tmp}"
TMP_ROOT="$(mktemp -d "$TMP_PARENT/cadence-precommit.XXXXXXXX")" || {
    printf '[Cadence] Ошибка: не удалось создать временный каталог.\n' >&2
    exit 2
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    rm -rf -- "$TMP_ROOT"
    exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

die_refused() {
    printf '[Cadence] Отказ: %s\n' "$*" >&2
    exit 1
}

die_error() {
    printf '[Cadence] Ошибка: %s\n' "$*" >&2
    exit 2
}

[[ -f "$CHECK_EXPORT" ]] || \
    die_error "не найден check_capture_export.py: $CHECK_EXPORT"

[[ -f "$PARSE_NETLIST" ]] || \
    die_error "не найден parse_capture_netlist.py: $PARSE_NETLIST"

[[ -f "$COMPARE_NETLISTS" ]] || \
    die_error "не найден compare_netlists.py: $COMPARE_NETLISTS"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || \
    die_error "не найден Python: $PYTHON_BIN"

[[ -d "$EXPORT_DIR" ]] || {
    printf '[Cadence] Каталог экспорта отсутствует: %s\n' \
        "$EXPORT_DIR" >&2
    printf '[Cadence] Сначала выполните экспорт нетлиста Capture в этот каталог.\n' \
        >&2
    exit 1
}

printf '[Cadence] Проверка результата экспорта.\n'

"$PYTHON_BIN" "$CHECK_EXPORT" \
    --input-dir "$EXPORT_DIR"

check_status=$?

case "$check_status" in
    0)
        ;;
    1)
        printf '[Cadence] Capture сообщил об отказе или неполном комплекте.\n' \
            >&2
        exit 1
        ;;
    2)
        printf '[Cadence] Ошибка проверки лога или файлов экспорта.\n' >&2
        exit 2
        ;;
    *)
        printf '[Cadence] Неожиданный код check_capture_export.py: %s\n' \
            "$check_status" >&2
        exit 2
        ;;
esac

CANDIDATE_JSON="$TMP_ROOT/candidate.json"
COMPARE_REPORT="$TMP_ROOT/compare.json"

printf '[Cadence] Разбор DAT-файлов.\n'

"$PYTHON_BIN" "$PARSE_NETLIST" \
    --input-dir "$EXPORT_DIR" \
    --output "$CANDIDATE_JSON"

parse_status=$?

case "$parse_status" in
    0)
        ;;
    1)
        printf '[Cadence] Парсер обнаружил ошибки в структуре нетлиста.\n' \
            >&2
        exit 1
        ;;
    2)
        printf '[Cadence] Ошибка запуска или чтения парсера.\n' >&2
        exit 2
        ;;
    *)
        printf '[Cadence] Неожиданный код parse_capture_netlist.py: %s\n' \
            "$parse_status" >&2
        exit 2
        ;;
esac

mkdir -p "$(dirname "$BASELINE_FILE")" || \
    die_error "не удалось подготовить каталог базовой модели"

BASELINE_JSON="$TMP_ROOT/baseline.json"

has_head=0
if git rev-parse --verify HEAD >/dev/null 2>&1; then
    has_head=1
fi

baseline_available=0

if [[ "$has_head" -eq 1 ]] &&
    git cat-file -e "HEAD:$BASELINE_RELATIVE" 2>/dev/null; then

    git show "HEAD:$BASELINE_RELATIVE" > "$BASELINE_JSON" || \
        die_error "не удалось получить базовую модель из HEAD"

    baseline_available=1
fi

if [[ "$baseline_available" -eq 0 ]]; then
    printf '[Cadence] Базовая модель в HEAD отсутствует.\n'
    printf '[Cadence] Текущая модель будет сохранена как первая базовая версия.\n'

    cp -- "$CANDIDATE_JSON" "$BASELINE_FILE" || \
        die_error "не удалось сохранить первую базовую модель"

    git add -- "$BASELINE_RELATIVE" || \
        die_error "не удалось добавить базовую модель в индекс"

    printf '[Cadence] Первая базовая модель добавлена: %s\n' \
        "$BASELINE_RELATIVE"

    exit 0
fi

printf '[Cadence] Сравнение с моделью из HEAD.\n'

"$PYTHON_BIN" "$COMPARE_NETLISTS" \
    "$BASELINE_JSON" \
    "$CANDIDATE_JSON" \
    --output "$COMPARE_REPORT"

compare_status=$?

case "$compare_status" in
    0)
        printf '[Cadence] Электрических изменений не обнаружено.\n'
        ;;
    1)
        printf '[Cadence] Обнаружены изменения нетлиста.\n'
        ;;
    2)
        printf '[Cadence] Ошибка сравнения нетлистов.\n' >&2
        exit 2
        ;;
    *)
        printf '[Cadence] Неожиданный код compare_netlists.py: %s\n' \
            "$compare_status" >&2
        exit 2
        ;;
esac

[[ -s "$COMPARE_REPORT" ]] || \
    die_error "compare_netlists.py не создал отчёт"

uncertainty_status="$(
    "$PYTHON_BIN" - "$COMPARE_REPORT" <<'PY'
import json
import sys

path = sys.argv[1]

try:
    with open(path, "r", encoding="utf-8") as stream:
        report = json.load(stream)
except Exception as exc:
    print(f"cannot read comparison report: {exc}", file=sys.stderr)
    raise SystemExit(2)

summary = report.get("summary")
if not isinstance(summary, dict):
    print("comparison report has no summary", file=sys.stderr)
    raise SystemExit(2)

if summary.get("has_uncertainty") is True:
    raise SystemExit(1)

raise SystemExit(0)
PY
)"
uncertainty_status=$?

case "$uncertainty_status" in
    0)
        ;;
    1)
        printf '[Cadence] Сравнение содержит неопределённые сопоставления.\n' \
            >&2
        printf '[Cadence] Коммит заблокирован до проверки отчёта.\n' >&2
        exit 1
        ;;
    2)
        die_error "некорректный отчёт compare_netlists.py"
        ;;
    *)
        die_error "неожиданный результат проверки отчёта"
        ;;
esac

cp -- "$CANDIDATE_JSON" "$BASELINE_FILE" || \
    die_error "не удалось обновить базовую модель"

git add -- "$BASELINE_RELATIVE" || \
    die_error "не удалось добавить обновлённую базовую модель в индекс"

printf '[Cadence] Базовая модель обновлена и добавлена в индекс: %s\n' \
    "$BASELINE_RELATIVE"

exit 0
