#!/bin/sh
# Установка и обновление точечного замера мест на VPS. Код — /opt/tabloda-seats,
# журналы — /var/lib/tabloda-seats (StateDirectory). Токен для выгрузки берётся
# у диспетчера: /opt/tabloda-dispatch/dispatch.env (нужно право Contents: write).
#
#   curl -fsSL https://raw.githubusercontent.com/SeanKOFF/uz_rail_board/main/deploy/install-seats.sh -o install-seats.sh
#   sudo sh install-seats.sh
set -eu
DIR=${DIR:-/opt/tabloda-seats}
RAW=${RAW:-https://raw.githubusercontent.com/SeanKOFF/uz_rail_board/main}
UNITS=${UNITS:-/etc/systemd/system}

[ "$(id -u)" = 0 ] || { echo "✗ запускать через sudo"; exit 1; }
mkdir -p "$DIR/units"
for f in seats_watch.py stations.json deploy/seats_windows.py; do
  curl -fsSL "$RAW/$f" -o "$DIR/$(basename $f).new" && mv "$DIR/$(basename $f).new" "$DIR/$(basename $f)"
done
for u in tabloda-seats.service tabloda-seats-upload.service tabloda-seats-upload.timer; do
  curl -fsSL "$RAW/deploy/$u" -o "$DIR/units/$u"
done
chmod 644 "$DIR"/*.py "$DIR"/*.json
python3 -c "import ast; [ast.parse(open('$DIR/'+f).read()) for f in ('seats_watch.py','seats_windows.py')]"
(cd "$DIR" && python3 seats_windows.py --plan)

[ -s /opt/tabloda-dispatch/dispatch.env ] || echo "! нет /opt/tabloda-dispatch/dispatch.env — выгрузка в репозиторий работать не будет"
cp "$DIR"/units/* "$UNITS/"
systemctl daemon-reload
systemctl enable tabloda-seats.service tabloda-seats-upload.timer >/dev/null
systemctl restart tabloda-seats.service
systemctl start tabloda-seats-upload.timer
sleep 3
systemctl --no-pager --lines=5 status tabloda-seats.service || true
echo "✓ готово. Журнал службы: journalctl -u tabloda-seats -f"
echo "  Данные: /var/lib/tabloda-seats/ДАТА.jsonl, выгрузка вчерашнего — ежедневно в 12:10"
