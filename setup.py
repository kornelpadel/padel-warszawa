"""
setup.py – Uruchom mnie RAZ żeby zainstalować wszystko czego potrzeba.
Potem już tylko: python scraper_browser.py
"""
import subprocess, sys

def run(cmd, desc):
    print(f"\n⏳ {desc}...")
    result = subprocess.run(cmd, shell=True)
    if result.returncode == 0:
        print(f"✓ OK")
    else:
        print(f"✗ Błąd! Skopiuj powyższy komunikat i wyślij mi.")
        sys.exit(1)

print("=" * 50)
print("  Instalacja – Skraper kortów padlowych Warszawa")
print("=" * 50)

run(f"{sys.executable} -m pip install requests beautifulsoup4 lxml playwright",
    "Instaluję biblioteki Python")

run(f"{sys.executable} -m playwright install chromium",
    "Pobierам przeglądarkę Chromium (jednorazowe ~150 MB)")

print("\n" + "=" * 50)
print("✅ Wszystko gotowe!")
print("\nAby zebrać dane uruchom:")
print("    python scraper_browser.py")
print("=" * 50 + "\n")
