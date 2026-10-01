#!/bin/sh
# Установка и обновление диспетчера запусков на VPS. Всё — в одной папке
# /opt/tabloda-dispatch, файлы берутся из репозитория на GitHub (ветка main).
#
#   curl -fsSL https://raw.githubusercontent.com/SeanKOFF/uz_rail_board/main/deploy/install-dispatch.sh -o install-dispatch.sh
#   sudo sh install-dispatch.sh
#
# Повторный запуск обновляет скрипт и юниты, токен не трогает.
set -eu
DIR=${DIR:-/opt/tabloda-dispatch}
RAW=${RAW:-https://raw.githubusercontent.com/SeanKOFF/uz_rail_board/main/deploy}
TIMERS="seats-watch live-sync flights-sync suburban-sync buses-watch"

[ "$(id -u)" = 0 ] || { echo "✗ запускать через sudo"; exit 1; }
mkdir -p "$DIR/units"
for f in gh_dispatch.py gh-dispatch@.service; do
  curl -fsSL "$RAW/$f" -o "$DIR/$f.new" && mv "$DIR/$f.new" "$DIR/$f"
done
for t in $TIMERS; do
  curl -fsSL "$RAW/gh-dispatch-$t.timer" -o "$DIR/units/gh-dispatch-$t.timer"
done
mv "$DIR/gh-dispatch@.service" "$DIR/units/"
python3 -c "import ast,sys; ast.parse(open('$DIR/gh_dispatch.py').read())"

if [ ! -s "$DIR/dispatch.env" ]; then
  printf "GitHub fine-grained токен (Actions: Read and write): "
  stty -echo; read -r TOKEN; stty echo; echo
  (umask 077; echo "GH_TOKEN=$TOKEN" > "$DIR/dispatch.env")
fi
chmod 600 "$DIR/dispatch.env"

env $(cat "$DIR/dispatch.env") python3 "$DIR/gh_dispatch.py" --check || {
  echo "✗ токен не прошёл проверку, таймеры не включены."
  echo "  Чтобы ввести другой: sudo rm $DIR/dispatch.env и запустить установку снова."
  exit 1; }

cp "$DIR"/units/* /etc/systemd/system/
systemctl daemon-reload
for t in $TIMERS; do systemctl enable --now "gh-dispatch-$t.timer" >/dev/null; done
systemctl list-timers 'gh-dispatch*' --no-pager
echo "✓ готово. Журнал: journalctl -u 'gh-dispatch@*' --since today"
