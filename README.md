# ניטור מלאי PS5 בחנויות בישראל

סקריפט Python (ספרייה סטנדרטית בלבד) שבודק חנויות כל 10 דקות, ושולח התראה ל-ntfy כשקונסולת PS5 עוברת ל"במלאי".

## התקנה בשרת

1. משוך את הקוד מהענף שבו הוא נמצא:

```bash
git clone -b claude/ps5-stock-monitor-lm31ka https://github.com/omercc-glitch/ps5-stock-monitor.git
cd ps5-stock-monitor
```

2. הרץ בדיקה ידנית, שמראה אילו חנויות נפתחות מהכתובת הביתית שלך:

```bash
python3 monitor.py --check
```

3. התקן את הטיימר. הסקריפט ישאל על שם הנושא ב-ntfy:

```bash
./install.sh
```

4. שלח התראת ניסוי, ווודא שהיא הגיעה לטלפון:

```bash
python3 monitor.py --test-notify
```

5. כדי שהטיימר ירוץ גם כשאף משתמש לא מחובר:

```bash
sudo loginctl enable-linger $USER
```

## מה הסקריפט עושה

- התראה נשלחת רק כשמוצר עובר מ"אזל" ל"במלאי", או כשמוצר חדש מופיע כשהוא כבר במלאי.
- חנות שנכשלת 3 ריצות ברצף (כ-30 דקות) מקבלת התראה אחת על התקלה. חנויות מסומנות `experimental` לא שולחות התראות תקלה.
- פעם בשבוע נשלחת הודעה שקטה "ניטור פעיל", כדי שתדע שהמערכת חיה.
- שם הנושא נשמר רק ב-`~/.config/ps5-monitor/env` ולא בקוד.
- המצב נשמר ב-`~/.local/state/ps5-monitor/state.json`.

## חנויות

ההגדרות ב-`stores.json`.

| חנות | שיטה |
|---|---|
| ישפאר, קרביץ | רשימת מוצרים של Shopify |
| Ivory | נתונים מובנים (JSON-LD) בעמוד הקטגוריה |
| Bug | דפי מוצר ידועים, וגילוי דפי קונסולה חדשים בעמוד הקונסולות |
| KSP, מחסני חשמל, א.ל.מ, CWC | `experimental`. נחסמו מהענן, וייתכן שהפרסר הכללי לא יתאים להם. הבדיקה הידנית תראה |

האחריות מוצגת בהתראה כהערה. הסקריפט לא מאמת אחריות יבואן, וצריך לוודא אותה מול החנות לפני קנייה.

## פקודות שימושיות

```bash
systemctl --user list-timers ps5-monitor.timer
journalctl --user -u ps5-monitor.service -n 50 --no-pager
systemctl --user start ps5-monitor.service
python3 -m unittest discover -s tests
```

## הערות

- הסקריפט מזדהה בכותרת User-Agent כ-`ps5-stock-monitor` ולא מנסה לעקוף חסימות. חנות שחוסמת תוצג כ"חסומה".
- כל 10 דקות הוא ערך שנבחר כפשרה, ולא על סמך נתונים. אפשר לשנות ב-`OnUnitActiveSec` בקובץ הטיימר.
