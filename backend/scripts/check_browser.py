"""Check local Chrome without visiting external pages or using login data."""

from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="chrome", headless=True)
    try:
        page = browser.new_page()
        page.set_content('<input aria-label="Price"><button>Check</button>')
        page.get_by_label("Price").fill("1.2345")
        assert page.get_by_label("Price").input_value() == "1.2345"
        page.get_by_role("button", name="Check").click()
        print(f"Chrome {browser.version}: launch, fill and click passed")
    finally:
        browser.close()
