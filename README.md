# XD Downloader — دانلودر ویدیو از ۱۰۰۰+ سایت (ربات تلگرام)

ربات تلگرام شخصی برای دانلود ویدیو/صدا از یوتیوب، X، اینستاگرام، تیک‌تاک، ردیت و هر سایتی که yt-dlp ساپورت کنه.

**موتور:** yt-dlp (همون موتور ReClip) + ffmpeg  
**رابط:** ربات تلگرام با انتخاب کیفیت (اینلاین کیبورد)

---

## 🚀 نصب روی هاست شخصی

### پیش‌نیازها
```bash
sudo apt update && sudo apt install -y ffmpeg python3 python3-pip
```

### ۱. کلون کن
```bash
git clone https://github.com/aparouz3/xd-downloader.git
cd xd-downloader
```

### ۲. وابستگی‌ها
```bash
pip3 install -r requirements.txt
```

### ۳. توکن ربات
```bash
echo "YOUR_BOT_TOKEN_HERE" > token.txt
# یا از طریق ENV:
export XD_BOT_TOKEN="YOUR_BOT_TOKEN_HERE"
```

### ۴. آیدی کاربر خودت
```bash
export ALLOWED_USER_ID="5669842121"
# یا داخل bot.py خط ALLOWED_USER_ID رو عوض کن
```

### ۵. اجرا (تست)
```bash
python3 bot.py
```

### ۶. سرویس دائمی (systemd)
```bash
sudo cp xd-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xd-bot.service
```

---

## 📱 استفاده

- یه لینک بفرست به ربات → کیفیت رو انتخاب کن → ویدیو تحویل بگیر
  - **480p / 720p / 1080p / Best**
- `/mp3 <لینک>` → فقط صدا (بدون انتخاب کیفیت)

---

## ⚙️ متغیرهای محیطی

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `XD_BOT_TOKEN` | (اجباری) | توکن ربات از @BotFather |
| `ALLOWED_USER_ID` | `5669842121` | فقط این کاربر می‌تونه استفاده کنه |
| `XD_DOWNLOAD_DIR` | `./downloads` | محل ذخیره موقت فایل‌ها |

---

## 🔒 امنیت

- فقط `ALLOWED_USER_ID` مجاز به استفاده از رباته
- فایل‌های دانلود شده بعد از ارسال پاک میشن
- توکن در `XD_BOT_TOKEN` یا `token.txt` (به git نمی‌ره)

---

## 📄 لایسنس
MIT