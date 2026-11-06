"""Actual registration UI, HTTP bridge and shared-account login; synthetic email only."""
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright,expect
from registration_tests import RegistrationFixture,PAYLOAD


def main():
    with RegistrationFixture() as fixture,sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        try:
            page=browser.new_page(viewport={'width':390,'height':844});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.add_init_script('window.addEventListener("alchemyhatchery:authenticated",e=>window.fixtureUser=e.detail.user)')
            page.goto(fixture.base+'/fixture')
            page.locator('.auth-tabs [data-auth-tab=register]').click()
            expect(page.locator('#registrationCampus')).to_be_visible()
            page.locator('#registrationCampusId').fill('unknown')
            page.locator('#registrationCampus [type=submit]').click()
            expect(page.locator('#registrationCampusError')).to_contain_text('未开放注册')
            expect(page.locator('#registrationInfo')).to_be_hidden()
            page.locator('#registrationCampusId').fill(PAYLOAD['campusId'])
            page.locator('#registrationCampus [type=submit]').click()
            expect(page.locator('#registrationInfo')).to_be_visible()
            expect(page.locator('#registrationCampus')).to_be_hidden()
            expect(page.locator('#registrationSendOtp')).to_have_text('发送验证码')
            assert page.locator('#registrationInfo [name=grade] option').all_text_contents()==['请选择年级','初一','初二','初三','高一','高二','高三','毕业年级']
            assert page.locator('#registrationInfo [name=classGroup] option').count()==25
            assert page.locator('#registrationInfo input').evaluate_all('(nodes)=>nodes.map(n=>n.name)')==['name','realName','email','code','password','confirmPassword']
            for name in ('name','realName','email','password'):
                page.locator(f'#registrationInfo [name={name}]').fill(PAYLOAD[name])
            for name in ('grade','classGroup'):
                page.locator(f'#registrationInfo [name={name}]').select_option(PAYLOAD[name])
            page.locator('#registrationInfo [name=confirmPassword]').fill('mismatch')
            expect(page.locator('#registrationMismatch')).to_be_visible()
            expect(page.locator('#registrationInfo [type=submit]')).to_be_disabled()
            fixture.otp_entered=threading.Event();fixture.otp_release=threading.Event()
            page.locator('#registrationSendOtp').click();assert fixture.otp_entered.wait(2)
            page.locator('#registrationEmail').fill('changed@example.test')
            fixture.otp_release.set()
            expect(page.locator('#registrationSendOtp')).to_have_text('60s')
            expect(page.locator('#registrationOtpHint')).to_be_hidden()
            expect(page.locator('#registrationInfo [type=submit]')).to_be_disabled()
            assert len([c for c in fixture.calls if c['path'].endswith('/send-otp')])==1
            page.locator('#registrationBack').click()
            expect(page.locator('#registrationCampus')).to_be_visible()
            page.locator('#registrationCampusId').fill(PAYLOAD['campusId'])
            page.locator('#registrationCampus [type=submit]').click()
            expect(page.locator('#registrationInfo')).to_be_visible()
            page.locator('#registrationEmail').fill(PAYLOAD['email'])
            # Move only the browser's time forward to test the actual cooldown,
            # without waiting a minute or changing the server rate limiter.
            page.evaluate('()=>{const original=Date.now;Date.now=()=>original()+61000}')
            expect(page.locator('#registrationSendOtp')).to_be_enabled()
            fixture.otp_entered=fixture.otp_release=None
            page.locator('#registrationSendOtp').dblclick()
            expect(page.locator('#registrationOtpHint')).to_be_visible()
            assert len([c for c in fixture.calls if c['path'].endswith('/send-otp')])==2
            assert '开发模式验证码' not in page.locator('#registerForm').inner_text()
            page.locator('#registrationInfo [name=code]').fill('a1234567')
            expect(page.locator('#registrationInfo [name=code]')).to_have_value('12345')
            page.locator('#registrationInfo [name=code]').fill('000000')
            page.locator('#registrationInfo [name=confirmPassword]').fill(PAYLOAD['password'])
            page.locator('#registrationTogglePassword').click()
            expect(page.locator('#registrationInfo [name=password]')).to_have_attribute('type','text')
            expect(page.locator('#registrationInfo [name=confirmPassword]')).to_have_attribute('type','text')
            for width in (320,390,1440):
                page.set_viewport_size({'width':width,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
                page.locator('#registrationInfo [name=name]').scroll_into_view_if_needed()
                expect(page.locator('#registrationInfo [name=name]')).to_be_in_viewport()
                page.locator('#registrationBack').scroll_into_view_if_needed()
                expect(page.locator('#registrationBack')).to_be_in_viewport()
            page.locator('#registrationInfo [type=submit]').click()
            expect(page.locator('#registrationError')).to_contain_text('验证码无效')
            expect(page.locator('#registrationInfo [name=name]')).to_have_value(PAYLOAD['name'])
            expect(page.locator('#registrationInfo [name=password]')).to_have_value(PAYLOAD['password'])
            fixture.override=(502,{'error':'fixture outage'},{})
            page.locator('#registrationInfo [type=submit]').click()
            expect(page.locator('#registrationError')).to_contain_text('结果暂未确认')
            page.wait_for_timeout(400)
            assert len([c for c in fixture.calls if c['path'].endswith('/register')])==2
            fixture.override=None
            page.locator('#registrationInfo [name=code]').fill(PAYLOAD['code'])
            page.locator('#registrationInfo [type=submit]').dblclick()
            expect(page.locator('#registrationSuccess')).to_contain_text('注册成功')
            expect(page.locator('#loginForm')).to_be_visible()
            expect(page.locator('#registerForm')).to_be_hidden()
            expect(page.locator('#loginUsername')).to_have_value(PAYLOAD['campusId'])
            assert len([c for c in fixture.calls if c['path'].endswith('/register')])==3
            page.locator('#loginPassword').fill(PAYLOAD['password'])
            page.locator('#loginForm [type=submit]').click()
            page.wait_for_function('window.fixtureUser?.username==="公开昵称"')
            assert not errors,errors
            print(json.dumps({'passed':['two-step eligibility gate','matching fields and options','stale OTP response ignored','cooldown and duplicate click guard','email edit clears code','back preserves profile','mobile layout and scrolling','wrong code preserves inputs','uncertain registration is not replayed','shared account logs in after registration'],'page_errors':errors,'real_emails':0},ensure_ascii=False))
        finally:browser.close()


if __name__=='__main__':main()
