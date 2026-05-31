#!/bin/bash
# Uruchom ten skrypt RAZ żeby zapisać hasło w bezpiecznym Keychain macOS
# Potem scraper.py będzie działał automatycznie bez pytania o hasło

EMAIL="korneltennis@gmail.com"
SERVICE="kluby_org"

echo "=== Zapisywanie hasła kluby.org do macOS Keychain ==="
echo ""
echo "Email: $EMAIL"
echo ""

# Usuń stare hasło jeśli istnieje
security delete-generic-password -a "$EMAIL" -s "$SERVICE" 2>/dev/null

# Dodaj nowe (zapyta o hasło)
security add-generic-password -a "$EMAIL" -s "$SERVICE" -w

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Hasło zapisane! Scraper będzie teraz logował się automatycznie."
    echo ""
    echo "Aby ustawić automatyczne zbieranie danych codziennie o 7:00:"
    echo ""
    echo "  crontab -e"
    echo ""
    echo "Wpisz tę linię i zapisz (Ctrl+O, Enter, Ctrl+X):"
    echo ""
    echo "  0 7 * * * cd ~/Desktop/padel && /usr/local/bin/python3 scraper.py >> scraper_cron.log 2>&1"
    echo ""
else
    echo "❌ Błąd zapisu. Spróbuj ponownie."
fi
