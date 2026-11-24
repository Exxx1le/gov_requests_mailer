#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

fail() {
    printf 'Ошибка: %s\n' "$1" >&2
    exit 1
}

candidate=${SUDO_USER:-}
case "${1:-}" in
    -h|--help)
        printf 'Использование: sudo bash deploy/install.sh [--user ПОЛЬЗОВАТЕЛЬ]\n'
        exit 0
        ;;
    --user)
        (( $# == 2 )) || fail "Укажите пользователя после --user."
        candidate=$2
        ;;
    '')
        ;;
    *)
        fail "Неизвестный аргумент. См. deploy/install.sh --help."
        ;;
esac

(( EUID == 0 )) || fail "Запустите установщик через sudo."
[[ -n "$candidate" ]] || fail "Укажите непривилегированного пользователя через --user."
account=$(getent -- passwd "$candidate") || fail "Указанный пользователь не найден."
IFS=: read -r service_user _ service_uid _ _ _ _ <<< "$account"
[[ "$service_uid" =~ ^[0-9]+$ ]] && (( service_uid != 0 )) \
    || fail "Запуск рассылки от root запрещён."

installer_path=$(readlink -f -- "${BASH_SOURCE[0]}")
deploy_dir=${installer_path%/*}
project_dir=${deploy_dir%/*}
[[ "$project_dir" != *[[:cntrl:]]* ]] || fail "Путь к проекту содержит управляющие символы."
python_bin="$project_dir/.venv/bin/python"
cd -- "$project_dir"

as_service_user() {
    /usr/sbin/runuser -u "$service_user" -- "$@"
}

command -v systemd-analyze >/dev/null && command -v systemd-cat >/dev/null \
    || fail "Установите systemd на Ubuntu."
/usr/bin/systemctl show -p Version --value >/dev/null \
    || fail "Системный менеджер systemd недоступен."
[[ -f main.py && -f recipients.py && -f requirements.txt ]] || fail "Файлы проекта отсутствуют."
[[ -f .venv/pyvenv.cfg ]] && as_service_user /usr/bin/test -x "$python_bin" \
    || fail "Создайте .venv в проекте и установите зависимости из requirements.txt."
as_service_user /usr/bin/test -w "$project_dir" && as_service_user /usr/bin/test -x "$project_dir" \
    || fail "Пользователь $service_user должен иметь право записи в каталог проекта."
if [[ -e generated || -L generated ]]; then
    [[ -d generated && ! -L generated ]] && as_service_user /usr/bin/test -w generated \
        || fail "Пользователю $service_user нужен доступ на запись в обычный каталог generated/."
fi

[[ -f .env ]] || fail "Заполните .env по образцу .env.example."
[[ -f recipients.json ]] \
    || fail "Создайте список адресатов через recipients.py add EMAIL или recipients.py migrate."
for file in .env recipients.json mailing_history.json recipients.json.lock mailing_history.json.lock; do
    if [[ ! -e "$file" && ! -L "$file" ]]; then
        continue
    fi
    [[ -f "$file" && ! -L "$file" && $(stat -c '%u' -- "$file") == "$service_uid" ]] \
        || fail "$file должен быть обычным файлом, принадлежащим пользователю $service_user."
    as_service_user /usr/bin/chmod 0600 -- "$file" || fail "Не удалось защитить файл $file."
done

as_service_user /usr/bin/env -u PYTHONHOME -u PYTHONPATH "$python_bin" -B -m pip check \
    || fail "Проверьте зависимости виртуального окружения."
as_service_user /usr/bin/env -u PYTHONHOME -u PYTHONPATH -u PYTHON_DOTENV_DISABLED \
    -u EMAIL_HOST -u EMAIL_PORT -u EMAIL_USER -u EMAIL_PASSWORD "$python_bin" -B - <<'PY'
from zoneinfo import ZoneInfo

try:
    from importlib.metadata import version
    import utils
    from email_sender import _load_email_settings

    for dependency in ("requests", "python-docx", "python-dotenv", "tzdata"):
        version(dependency)
    ZoneInfo("Europe/Moscow")
except Exception:
    raise SystemExit("Ошибка: установите зависимости из requirements.txt.") from None

try:
    _load_email_settings()
except Exception:
    raise SystemExit("Ошибка: проверьте настройки SMTP в .env и адресатов в recipients.json.") from None
PY

stage=$(mktemp -d)
trap 'rm -f -- "$stage/gov-mailer.service" "$stage/gov-mailer.timer" "$stage/mailer"; rmdir -- "$stage"' EXIT

# WorkingDirectory uses the entire value; ExecStart has separate argument quoting.
unit_directory=${project_dir//%/%%}
unit_python=${python_bin//%/%%}
unit_python=${unit_python//\\/\\\\}
unit_python=${unit_python//\"/\\\"}
while IFS= read -r line; do
    case "$line" in
        User=@SERVICE_UID@) printf 'User=%s\n' "$service_uid" ;;
        WorkingDirectory=@PROJECT_DIR@/.) printf 'WorkingDirectory=%s/.\n' "$unit_directory" ;;
        'ExecStart=:/usr/bin/env -- "@PYTHON_BIN@" -B main.py')
            printf 'ExecStart=:/usr/bin/env -- "%s" -B main.py\n' "$unit_python"
            ;;
        *) printf '%s\n' "$line" ;;
    esac
done < "$deploy_dir/gov-mailer.service" > "$stage/gov-mailer.service"
cp -- "$deploy_dir/gov-mailer.timer" "$stage/gov-mailer.timer"
{
    printf '#!/bin/bash\n'
    printf 'readonly project_dir=%q\n' "$project_dir"
    printf 'readonly service_user=%q\n' "$service_user"
    printf 'readonly service_uid=%q\n' "$service_uid"
    tail -n +2 -- "$deploy_dir/mailer"
} > "$stage/mailer"

/bin/bash -n "$stage/mailer"
systemd-analyze verify "$stage/gov-mailer.service" "$stage/gov-mailer.timer"
install -o root -g root -m 0644 -- "$stage/gov-mailer.service" "$stage/gov-mailer.timer" /etc/systemd/system/
install -o root -g root -m 0755 -- "$stage/mailer" /usr/local/bin/mailer
/usr/bin/systemctl daemon-reload

printf 'Установлено для пользователя %s. Для включения таймера выполните mailer enable.\n' "$service_user"
