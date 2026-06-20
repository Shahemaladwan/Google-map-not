#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
 Daleli.sa Lead Hunter — نظام صيد المتاجر غير الموجودة على خرائط جوجل
==============================================================================

الفكرة:
  1) يزحف على Daleli.sa (دليل أعمال سعودي عام) ويستخرج المتاجر التي تملك
     رقم هاتف ظاهر في قائمة النتائج.
  2) يتحقق من كل متجر عبر سحب نتائج بحث خرائط جوجل مباشرة (Playwright،
     بدون أي API مدفوع أو مفتاح) باستخدام Fuzzy Matching على الاسم.
  3) أي متجر لا يظهر بشكل مطابق (score < 85) على الخريطة → يُعتبر عميل
     محتمل (Lead) ويُرسل تلقائياً إلى بوت تيليجرام مع رابط واتساب جاهز.

⚠️ مهم قبل التشغيل:
  - راجع `الأحكام والشروط` و `سياسة الخصوصية` الخاصة بـ Daleli.sa (وأي قواعد
    Terms of Service) للتأكد أن الاستخدام الآلي للبيانات مسموح به لحالتك.
    البيانات على الموقع عامة، لكن الزحف الآلي قد يكون مقيداً بشروط الموقع،
    وهذا تقييم تحتاج تتأكد منه أنت (المستخدم) حسب نيتك في الاستخدام.
  - سحب نتائج خرائط جوجل مباشرة (بدون API) يخالف شروط استخدام جوجل من
    الناحية الرسمية، وقد يعرّض الـ IP المستخدم للحظر المؤقت من جوجل لو
    الطلبات كثيرة/سريعة. هذا تنازل واعٍ مقابل تجنّب تكلفة/متطلبات بطاقة
    API الرسمي — استخدم تأخير معقول (افتراضياً مفعّل بالكود) وتجنب
    التشغيل بكثافة عالية أو من سيرفر واحد لساعات متواصلة.
  - لا ترسل رسائل واتساب جماعية غير مرغوبة (Spam) — القانون السعودي
    لمكافحة الرسائل المزعجة (CITC) يعاقب على ذلك. استخدم الرابط الناتج
    كنقطة بداية تواصل فردي مدروس، وليس إرسال جماعي آلي.

==============================================================================
متطلبات التثبيت:
  pip install playwright playwright-stealth thefuzz python-levenshtein requests
  playwright install chromium

متغيرات البيئة المطلوبة (أو عدّلها مباشرة في قسم CONFIG):
  TELEGRAM_BOT_TOKEN    -> توكن بوت تيليجرام (من BotFather)
  TELEGRAM_CHAT_ID      -> آيدي الشات/القناة اللي بتستقبل التنبيهات
==============================================================================
"""

import os
import re
import time
import random
import sqlite3
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests
from thefuzz import fuzz
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False


# ==============================================================================
# CONFIG — عدّل هذا القسم فقط في الأغلب
# ==============================================================================

class Config:
    BASE_URL = "https://daleli.sa"
    CATEGORIES_URL = f"{BASE_URL}/advertisements/categories-view"

    # حد أقصى لعدد صفحات التصنيفات (كل صفحة فيها ~24 تصنيف). الموقع فيه ~61 صفحة.
    MAX_CATEGORY_PAGES = 61

    # حد أقصى لعدد مرات الضغط على "تحميل المزيد" داخل كل تصنيف (للحماية من حلقات لا تنتهي)
    MAX_LOAD_MORE_CLICKS = 60

    # فترات الانتظار العشوائية (ثواني) بين الطلبات لتقليل احتمال الحظر
    SLEEP_MIN = 1.5
    SLEEP_MAX = 4.0

    # مهلة الانتظار القصوى لتحميل عنصر (مللي ثانية)
    NAV_TIMEOUT_MS = 30000

    # Headless أم لا (شغّله False أول مرة عشان تتابع وتتأكد من السلوك)
    HEADLESS = True

    # عتبة التطابق في Fuzzy Matching — أقل منها = "غير موجود رقمياً" = Lead
    FUZZY_MATCH_THRESHOLD = 85

    # الحد الأقصى لعدد المتاجر التي تتم معالجتها (فحص) في كل تشغيل واحد للسكربت
    MAX_STORES_PER_RUN = 100

    # قاعدة البيانات المحلية لمنع تكرار الفحص على نفس رقم الهاتف
    DB_PATH = "daleli_leads.db"

    # مهلة انتظار ظهور نتائج بحث خرائط جوجل (مللي ثانية)
    MAPS_RESULTS_TIMEOUT_MS = 12000

    # بيانات الاعتماد (يُفضّل من متغيرات البيئة)
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ضع_توكن_البوت_هنا")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "ضع_آيدي_الشات_هنا")


# ==============================================================================
# LOGGING
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daleli_hunter")


# ==============================================================================
# DATA MODEL
# ==============================================================================

@dataclass
class StoreListing:
    name: str
    category: str
    phone: str          # أول رقم هاتف نظيف (للاستخدام في واتساب / dedup)
    raw_phones: str      # كل الأرقام كما وردت (قد يكون فيها أكثر من رقم مفصولة بفواصل)
    page_url: str
    city: str = ""


def normalize_phone(raw: str) -> Optional[str]:
    """
    يحوّل رقم سعودي إلى صيغة دولية موحّدة بدون '+': 9665XXXXXXXX
    يرجّع None إذا الرقم غير صالح.
    """
    if not raw:
        return None
    # نأخذ أول رقم فقط إذا فيه عدة أرقام مفصولة بفواصل أو شرطات
    first = re.split(r"[,\-/]", raw.strip())[0].strip()
    digits = re.sub(r"\D", "", first)

    if digits.startswith("00966"):
        digits = digits[2:]
    if digits.startswith("966"):
        digits = digits[3:]
    if digits.startswith("0"):
        digits = digits[1:]

    # رقم جوال سعودي صحيح بعد التطبيع = يبدأ بـ 5 وطوله 9 أرقام
    if re.fullmatch(r"5\d{8}", digits):
        return "966" + digits

    # أرقام أرضية أو رقم 920 / 800 -- نتجاهلها لأغراض واتساب لكن نحتفظ فيها كمرجع
    return None


# ==============================================================================
# 1) دالة الزحف (Crawler) — جمع روابط/أسماء التصنيفات
# ==============================================================================

def get_category_search_urls(page) -> list:
    """
    يمر على صفحات (categories-view?page=N) ويستخرج روابط البحث
    (advertisements/search?keywords=...) لكل تصنيف.
    """
    category_urls = set()

    for page_num in range(1, Config.MAX_CATEGORY_PAGES + 1):
        url = f"{Config.CATEGORIES_URL}?page={page_num}"
        try:
            log.info(f"[تصنيفات] تحميل صفحة {page_num} ...")
            page.goto(url, timeout=Config.NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            random_sleep()

            links = page.eval_on_selector_all(
                "a[href*='/advertisements/search?keywords=']",
                "els => els.map(e => e.href)"
            )
            if not links:
                log.info("لا توجد تصنيفات إضافية، إيقاف الزحف على صفحات التصنيفات.")
                break

            before = len(category_urls)
            category_urls.update(links)
            log.info(f"  -> {len(links)} رابط في الصفحة، إجمالي فريد حتى الآن: {len(category_urls)}")

            if len(category_urls) == before:
                # ما زادت روابط جديدة، الأغلب وصلنا آخر صفحة فعلية
                pass

        except PlaywrightTimeoutError:
            log.warning(f"  ! انتهت المهلة في صفحة التصنيفات {page_num}, تخطي...")
            continue
        except Exception as e:
            log.warning(f"  ! خطأ غير متوقع في صفحة التصنيفات {page_num}: {e}")
            continue

    return list(category_urls)


def crawl_category(page, category_url: str) -> list:
    """
    يفتح صفحة نتائج تصنيف معيّن، يضغط 'تحميل المزيد' حتى النهاية،
    ثم يستخرج كل المتاجر التي تحتوي رقم هاتف ظاهر.
    (لا حاجة لدخول صفحة المتجر التفصيلية — البيانات كلها بالقائمة نفسها)
    """
    listings = []
    try:
        log.info(f"[تصنيف] فتح: {category_url}")
        page.goto(category_url, timeout=Config.NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        random_sleep()

        # الضغط على زر "تحميل المزيد" حتى يختفي أو نوصل الحد الأقصى
        clicks = 0
        while clicks < Config.MAX_LOAD_MORE_CLICKS:
            load_more = page.locator("text=تحميل المزيد")
            if load_more.count() == 0:
                break
            try:
                load_more.first.scroll_into_view_if_needed(timeout=5000)
                load_more.first.click(timeout=5000)
                clicks += 1
                random_sleep(short=True)
            except (PlaywrightTimeoutError, Exception):
                break

        listings = extract_listings_from_page(page, category_url)
        log.info(f"  -> تم استخراج {len(listings)} متجر (يملكون رقم هاتف ظاهر) من هذا التصنيف")

    except PlaywrightTimeoutError:
        log.warning(f"  ! انتهت المهلة عند فتح التصنيف: {category_url}")
    except Exception as e:
        log.warning(f"  ! خطأ أثناء الزحف على التصنيف {category_url}: {e}")

    return listings


def extract_listings_from_page(page, source_url: str) -> list:
    """
    يستخرج كل بطاقات المتاجر من DOM الصفحة الحالية.
    القاعدة الذهبية: تخطّي أي متجر بدون رقم هاتف ظاهر (tel: link).
    """
    results = []

    # كل بطاقة متجر = h5/h6 فيه رابط advertisements/<slug> + رابط tel: قريب منه
    cards = page.locator("a[href^='tel:']")
    count = cards.count()

    for i in range(count):
        try:
            tel_el = cards.nth(i)
            href = tel_el.get_attribute("href") or ""
            raw_phone = href.replace("tel:", "").strip()

            phone = normalize_phone(raw_phone)
            if not phone:
                # رقم غير صالح لواتساب (أرضي/مجاني) — نتجاهله حسب القاعدة الذهبية
                continue

            # نطلع لأقرب بطاقة (container) عشان نلقط اسم المتجر والتصنيف منها
            card = tel_el.locator(
                "xpath=ancestor::*[self::article or self::div][.//a[contains(@href,'/advertisements/')]][1]"
            )
            if card.count() == 0:
                continue
            card = card.first

            name_el = card.locator("h5 a, h6 a, h4 a").first
            if name_el.count() == 0:
                continue
            name = (name_el.inner_text() or "").strip()
            detail_url = name_el.get_attribute("href") or ""

            if not name or not detail_url:
                continue

            category_tags = card.locator("a[href*='/advertisements/search?keywords=']")
            categories = []
            for j in range(category_tags.count()):
                t = (category_tags.nth(j).inner_text() or "").strip()
                if t:
                    categories.append(t)
            category = " / ".join(categories[:2]) if categories else "غير محدد"

            results.append(StoreListing(
                name=name,
                category=category,
                phone=phone,
                raw_phones=raw_phone,
                page_url=urllib.parse.urljoin(Config.BASE_URL, detail_url),
            ))

        except Exception as e:
            log.debug(f"تخطي بطاقة بسبب خطأ استخراج: {e}")
            continue

    return results


# ==============================================================================
# 2) دالة الفحص (Validator) — التحقق عبر Google Places API + Fuzzy Matching
# ==============================================================================

def init_db():
    conn = sqlite3.connect(Config.DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checked_stores (
            phone TEXT PRIMARY KEY,
            name TEXT,
            category TEXT,
            page_url TEXT,
            found_on_maps INTEGER,
            match_score INTEGER,
            checked_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # جدول مستقل خاص فقط بـ"تم إرساله إلى تيليجرام فعلياً" — طبقة حماية ثانية
    # ومنفصلة عن جدول الفحص، حتى لو تغيّر منطق الفحص مستقبلاً يبقى منع
    # التكرار في الإرسال قائماً بذاته ولا يمكن تجاوزه.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_alerts (
            phone TEXT PRIMARY KEY,
            name TEXT,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def mark_as_sent_if_new(conn, store: StoreListing) -> bool:
    """
    يحاول تسجيل رقم الهاتف في جدول 'تم الإرسال'. يرجّع True فقط إذا كان
    هذا أول إرسال لهذا الرقم على الإطلاق (يُسمح بالإرسال). لو الرقم مُسجّل
    من قبل، يرجّع False فوراً (ممنوع الإرسال مهما كانت الظروف).
    """
    try:
        conn.execute(
            "INSERT INTO sent_alerts (phone, name) VALUES (?, ?)",
            (store.phone, store.name),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # الرقم موجود مسبقاً في جدول الإرسال = تم إرساله قبل كذا، ممنوع تكراره
        return False


def already_checked(conn, phone: str) -> bool:
    cur = conn.execute("SELECT 1 FROM checked_stores WHERE phone = ?", (phone,))
    return cur.fetchone() is not None


def save_check_result(conn, store: StoreListing, found_on_maps: bool, score: int):
    conn.execute(
        """INSERT OR REPLACE INTO checked_stores
           (phone, name, category, page_url, found_on_maps, match_score)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (store.phone, store.name, store.category, store.page_url, int(found_on_maps), score),
    )
    conn.commit()


def check_store_on_google_maps(maps_page, store: StoreListing) -> tuple:
    """
    يبحث عن المتجر في خرائط جوجل عبر متصفح Playwright منفصل (بدون أي API)
    ويقارن الاسم بأفضل نتيجة ظاهرة عبر Fuzzy Matching.
    يرجّع (found: bool, best_score: int).
    """
    query = f"{store.name} السعودية"
    search_url = f"https://www.google.com/maps/search/{urllib.parse.quote(query)}"

    try:
        maps_page.goto(search_url, timeout=Config.NAV_TIMEOUT_MS, wait_until="domcontentloaded")

        # قبول الكوكيز إن ظهرت (يظهر أحياناً حسب الموقع الجغرافي للـ IP)
        try:
            consent_btn = maps_page.locator(
                "button:has-text('Accept all'), button:has-text('قبول الكل'), "
                "button:has-text('I agree'), button:has-text('موافق')"
            )
            if consent_btn.count() > 0:
                consent_btn.first.click(timeout=3000)
                random_sleep(short=True)
        except Exception:
            pass

        # نتائج البحث تظهر إما كبطاقة واحدة (نتيجة مباشرة) أو كقائمة نتائج جانبية
        try:
            maps_page.wait_for_selector(
                "a.hfpxzc, h1.DUwDvf",
                timeout=Config.MAPS_RESULTS_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            # ما ظهرت أي نتيجة خلال المهلة = غير موجود
            return False, 0

        names_found = []

        # حالة 1: نتيجة مباشرة (صفحة مكان واحد فُتحت تلقائياً)
        direct_title = maps_page.locator("h1.DUwDvf")
        if direct_title.count() > 0:
            t = (direct_title.first.inner_text() or "").strip()
            if t:
                names_found.append(t)

        # حالة 2: قائمة نتائج جانبية (عدة بطاقات)
        result_links = maps_page.locator("a.hfpxzc")
        result_count = min(result_links.count(), 8)  # نكتفي بأول 8 نتائج
        for i in range(result_count):
            label = result_links.nth(i).get_attribute("aria-label") or ""
            label = label.strip()
            if label:
                names_found.append(label)

        if not names_found:
            return False, 0

        best_score = 0
        for candidate_name in names_found:
            score = fuzz.token_sort_ratio(store.name, candidate_name)
            if score > best_score:
                best_score = score

        found = best_score >= Config.FUZZY_MATCH_THRESHOLD
        return found, best_score

    except PlaywrightTimeoutError:
        log.warning(f"  ! انتهت المهلة أثناء البحث عن '{store.name}' في خرائط جوجل")
        raise
    except Exception as e:
        log.warning(f"  ! خطأ أثناء فحص '{store.name}' في خرائط جوجل: {e}")
        raise


# ==============================================================================
# 3) دالة التنبيه (Alert System) — واتساب + تيليجرام
# ==============================================================================

def build_whatsapp_link(store: StoreListing) -> str:
    message = f"السلام عليكم، هل هذا {store.name}؟"
    encoded_msg = urllib.parse.quote(message)
    return f"https://wa.me/{store.phone}?text={encoded_msg}"


def send_telegram_alert(store: StoreListing, whatsapp_link: str, counter: int, total: int):
    if Config.TELEGRAM_BOT_TOKEN in ("", "ضع_توكن_البوت_هنا"):
        log.warning("لم يتم ضبط TELEGRAM_BOT_TOKEN — تخطي إرسال التنبيه.")
        return

    text = (
        f"🔢 {counter} / {total}\n"
        f"📦 اسم المتجر: {store.name}\n"
        f"🏷️ نوع المتجر: {store.category}\n"
        f"📱 رابط التواصل: {whatsapp_link}"
    )

    url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": Config.TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        log.info(f"  ✅ تم إرسال Lead إلى تيليجرام: {store.name} ({counter}/{total})")
    except requests.RequestException as e:
        log.warning(f"  ! فشل إرسال تنبيه تيليجرام لـ '{store.name}': {e}")


# ==============================================================================
# أدوات مساعدة
# ==============================================================================

def random_sleep(short: bool = False):
    if short:
        time.sleep(random.uniform(0.6, 1.5))
    else:
        time.sleep(random.uniform(Config.SLEEP_MIN, Config.SLEEP_MAX))


def new_stealth_page(browser):
    context = browser.new_context(
        locale="ar-SA",
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    page = context.new_page()
    page.set_default_timeout(Config.NAV_TIMEOUT_MS)
    if STEALTH_AVAILABLE:
        stealth_sync(page)
    else:
        log.warning("playwright_stealth غير مثبتة — تشغيل بدون stealth (pip install playwright-stealth)")
    return context, page


# ==============================================================================
# التشغيل الرئيسي (Orchestrator)
# ==============================================================================

def run():
    conn = init_db()
    total_leads = 0
    total_checked = 0
    run_limit_reached = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=Config.HEADLESS)
        context, page = new_stealth_page(browser)
        maps_context, maps_page = new_stealth_page(browser)  # تبويب منفصل لخرائط جوجل

        try:
            log.info("=== المرحلة 1: جمع روابط كل التصنيفات ===")
            category_urls = get_category_search_urls(page)
            log.info(f"تم جمع {len(category_urls)} تصنيف فريد.\n")

            log.info(f"=== المرحلة 2: الزحف + الفحص + التنبيه (حد التشغيل: {Config.MAX_STORES_PER_RUN} متجر) ===")
            for idx, cat_url in enumerate(category_urls, start=1):
                if run_limit_reached:
                    break

                log.info(f"\n--- تصنيف {idx}/{len(category_urls)} ---")
                listings = crawl_category(page, cat_url)

                for store in listings:
                    if total_checked >= Config.MAX_STORES_PER_RUN:
                        run_limit_reached = True
                        log.info(f"\n🛑 تم الوصول للحد الأقصى ({Config.MAX_STORES_PER_RUN} متجر) لهذا التشغيل. إيقاف.")
                        break

                    try:
                        if already_checked(conn, store.phone):
                            log.info(f"  ⏭️  تم فحصه سابقاً، تخطي: {store.name}")
                            continue

                        random_sleep(short=True)
                        found, score = check_store_on_google_maps(maps_page, store)
                        total_checked += 1
                        save_check_result(conn, store, found, score)

                        progress = f"{total_checked} / {Config.MAX_STORES_PER_RUN}"

                        if not found:
                            # قفل صارم ضد الإرسال المكرر: حتى لو الرقم مرّ من أي
                            # مسار آخر بالغلط، الإرسال الفعلي يمر فقط مرة واحدة للأبد
                            if mark_as_sent_if_new(conn, store):
                                whatsapp_link = build_whatsapp_link(store)
                                send_telegram_alert(store, whatsapp_link, total_checked, Config.MAX_STORES_PER_RUN)
                                total_leads += 1
                                log.info(f"  🎯 [{progress}] LEAD: {store.name} (score={score})")
                            else:
                                log.info(f"  🚫 [{progress}] تم إرساله مسبقاً من قبل، تم تجاهله نهائياً: {store.name}")
                        else:
                            log.info(f"  ✔️  [{progress}] موجود على الخريطة: {store.name} (score={score})")

                    except Exception as e:
                        log.warning(f"  ! خطأ أثناء فحص '{store.name}': {e} — متابعة للمتجر التالي")
                        continue

        except KeyboardInterrupt:
            log.info("تم إيقاف السكربت يدوياً.")
        except Exception as e:
            log.error(f"خطأ عام غير متوقع في التشغيل: {e}")
        finally:
            context.close()
            maps_context.close()
            browser.close()
            conn.close()

    log.info(f"\n=== انتهى التشغيل | تم فحص {total_checked} متجر | تم إيجاد وإرسال {total_leads} Lead جديد ===")


if __name__ == "__main__":
    run()
