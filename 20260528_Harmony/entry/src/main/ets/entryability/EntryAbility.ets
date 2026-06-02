import { AbilityConstant, ConfigurationConstant, UIAbility, Want } from '@kit.AbilityKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { window } from '@kit.ArkUI';
import { webview } from '@kit.ArkWeb';
import { preferences } from '@kit.ArkData';
import { notificationManager } from '@kit.NotificationKit';

const DOMAIN = 0x0000;
const TARGET_URL: string = 'https://mail.hwaipy.cn/';
const COOKIE_TTL: string = '2592000';   // 30 days

export default class EntryAbility extends UIAbility {
  private prefs: preferences.Preferences | null = null;

  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    try { this.context.getApplicationContext().setColorMode(ConfigurationConstant.ColorMode.COLOR_MODE_NOT_SET); } catch (err) { }
    try { webview.WebCookieManager.putAcceptCookieEnabled(true); } catch (e) { }
    try { notificationManager.requestEnableNotification(this.context).catch(() => {}); } catch (e) { }
    if (TARGET_URL.length > 0) {
      try {
        const opt: preferences.Options = { name: 'webapp_cookies' };
        this.prefs = preferences.getPreferencesSync(this.context, opt);
        this.restoreCookies();
      } catch (e) {
        hilog.error(DOMAIN, 'CKE', 'prefs init failed: %{public}s', JSON.stringify(e));
      }
    }
  }

  private restoreCookies(): void {
    if (!this.prefs) { return; }
    try {
      const v: preferences.ValueType = this.prefs.getSync('cookies', '');
      const s: string = String(v);
      if (s.length === 0) { return; }
      const pairs: string[] = s.split(';');
      let n: number = 0;
      for (let i = 0; i < pairs.length; i++) {
        const kv: string = pairs[i].trim();
        if (kv.length === 0) { continue; }
        try {
          webview.WebCookieManager.configCookieSync(TARGET_URL, kv + '; Max-Age=' + COOKIE_TTL + '; Path=/');
          n++;
        } catch (e) { }
      }
      if (n > 0) { hilog.info(DOMAIN, 'CKR', 'restored %{public}d cookies', n); }
    } catch (e) {
      hilog.error(DOMAIN, 'CKE', 'restore failed: %{public}s', JSON.stringify(e));
    }
  }

  private saveCookies(): void {
    if (!this.prefs || TARGET_URL.length === 0) { return; }
    try {
      const c: string = webview.WebCookieManager.fetchCookieSync(TARGET_URL);
      if (c && c.length > 0) {
        this.prefs.putSync('cookies', c);
        this.prefs.flush().catch(() => { });
        hilog.info(DOMAIN, 'CKS', 'saved %{public}d chars', c.length);
      }
    } catch (e) {
      hilog.error(DOMAIN, 'CKE', 'save failed: %{public}s', JSON.stringify(e));
    }
  }

  onWindowStageCreate(windowStage: window.WindowStage): void {
    windowStage.loadContent('pages/Index', (err) => {
      if (err.code) { return; }
    });
  }

  onBackground(): void {
    try { webview.WebCookieManager.saveCookieAsync(); } catch (e) { }
    this.saveCookies();
  }
  onDestroy(): void {
    try { webview.WebCookieManager.saveCookieAsync(); } catch (e) { }
    this.saveCookies();
  }
}
