"""Run the fake server first, then .shots-venv/bin/python this file.

    .venv/bin/python scripts/ai_concurrency_harness.py --port 4182
    .shots-venv/bin/python scripts/ai_context_browser_tests.py

Only fake users, database and AI are used. No external model calls.
"""
import json
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:4182'
USAGE = {'session-a': 400000, 'session-b': 22000, 'session-c': 67000}


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 960})
        page = context.new_page()
        errors, sent = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: sent.append(request.post_data_json)
                if '/api/ai/run?async=' in request.url and request.method == 'POST' else None)
        page.add_init_script("localStorage.setItem('alchemyhatchery:tester:ai-model:v1', JSON.stringify({model:'k3',effort:'off'}))")

        def inject_usage(route):
            response = route.fetch()
            data = response.json()
            if data.get('result'):
                summary = data['result'].get('summary', '')
                for name, tokens in USAGE.items():
                    if name in summary:
                        data['result']['contextUsage'] = {'usedTokens': tokens, 'contextWindow': 1000000}
            route.fulfill(response=response, json=data)

        page.route('**/api/ai/run/status?*', inject_usage)
        context.request.get(BASE + '/__test/reset')
        page.goto(BASE + '/__test/login?as=tester')
        page.wait_for_function('currentConsoleUser !== null')
        assert page.locator('#aiModelBtn, #aiModelMenu, #aiEffortLabel').count() == 0
        assert page.locator('#aiModeBtn').count() == 1

        def tokens():
            return page.evaluate('aiContextUsage().usedTokens')

        def send(name, wait=True):
            page.locator('#aiPrompt').fill(name)
            page.locator('#aiAdjustBtn').click()
            page.wait_for_url('**/c/*')
            session_id = page.url.rsplit('/', 1)[1]
            if wait:
                page.wait_for_function('(n) => aiContextUsage().usedTokens === n', arg=USAGE[name])
            return session_id

        session_a = send('session-a')
        assert page.locator('#aiContextRingWrap').get_attribute('aria-valuenow') == '40'
        page.locator('#aiNewChatBtn').click()
        assert tokens() < 5000 and page.evaluate('aiContextUsage().estimated')
        session_b = send('session-b')
        assert tokens() == 22000
        page.locator(f'[data-ai-session="{session_a}"]').click()
        page.wait_for_function('aiContextUsage().usedTokens === 400000')
        page.locator(f'[data-ai-session="{session_b}"]').click()
        page.wait_for_function('aiContextUsage().usedTokens === 22000')

        page.locator('#aiNewChatBtn').click()
        context.request.post(BASE + '/__test/ai/config', data={'delayMs': 1600})
        session_c = send('session-c', wait=False)
        page.locator(f'[data-ai-session="{session_a}"]').click()
        page.wait_for_function('aiContextUsage().usedTokens === 400000')
        page.wait_for_function('!anyAiRunActive()')
        assert tokens() == 400000, 'Background completion overwrote the active session'
        page.locator(f'[data-ai-session="{session_c}"]').click()
        page.wait_for_function('aiContextUsage().usedTokens === 67000')
        page.evaluate('saveAiChatSession()')
        page.reload()
        page.wait_for_function('aiContextUsage().usedTokens === 67000')

        page.locator('#aiPrompt').fill('new input')
        assert page.evaluate('aiContextUsage().estimated')
        page.locator('#aiPrompt').fill('')
        assert tokens() == 67000
        page.locator('#aiNewChatBtn').click()
        assert tokens() < 5000 and page.evaluate('aiContextUsage().estimated')
        assert len(sent) == 3 and all('model' not in body and 'reasoningEffort' not in body for body in sent)

        # The current provider's key form sends only its own field; intercepted
        # here so testing cannot change any real key or administrator state.
        key_updates = []
        config = {'provider': 'deepseek', 'providerLabel': 'DeepSeek Harness',
                  'model': 'DeepSeek-V4.1-Flash', 'deepseekKeySet': True, 'deepseekKeyMask': '···1234'}

        def admin_response(route):
            if route.request.url.endswith('/api/admin/ai-config'):
                if route.request.method == 'POST':
                    key_updates.append(route.request.post_data_json)
                route.fulfill(json=config)
            else:
                route.fulfill(json={})

        page.route('**/api/admin/**', admin_response)
        page.evaluate("config => openModal('AI 服务配置', renderAiServiceSettings(config))", config)
        page.locator('[name="apiKey"]').fill('sk-fake-browser-key')
        page.locator('[data-console-form="ai-service-key"] button').first.click()
        page.wait_for_function("document.querySelector('[data-console-form=\"ai-service-key\"] [name=\"apiKey\"]')?.value === ''")
        assert key_updates == [{'deepseekApiKey': 'sk-fake-browser-key'}]
        page.evaluate('closeModal()')
        assert not errors, errors
        page.screenshot(path='/tmp/hatchery-ai-context.png')
        print(json.dumps({'passed': ['model picker removed', 'legacy selection ignored', 'session switching',
                                    'background completion isolation', 'refresh persistence', 'draft estimation',
                                    'new chat reset', 'DeepSeek admin key form', 'no JavaScript errors']}, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()
