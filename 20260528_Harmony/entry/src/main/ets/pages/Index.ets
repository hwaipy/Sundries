import { webview } from '@kit.ArkWeb';
import { promptAction } from '@kit.ArkUI';
import { common, wantConstant } from '@kit.AbilityKit';
import { fileUri } from '@kit.CoreFileKit';
import { window } from '@kit.ArkUI';
import { deviceInfo, batteryInfo, pasteboard } from '@kit.BasicServicesKit';
import { vibrator } from '@kit.SensorServiceKit';
import { preferences } from '@kit.ArkData';
import { systemShare } from '@kit.ShareKit';
import { uniformTypeDescriptor as utd } from '@kit.ArkData';
import { notificationManager } from '@kit.NotificationKit';

const APP_NAME: string = 'Mail';
const BUNDLE_NAME: string = 'com.hwaipy.webmail';

const HARMONY_SHIM: string = `(function(){
  var n = window.__hbridge;
  if(!n){ return; }
  var cbs = {};
  window.__hresolve = function(id, val){ if(cbs[id]){ try{cbs[id](val);}catch(e){} delete cbs[id]; } };
  function ac(fn){ return new Promise(function(res){ var id='cb'+Date.now()+'_'+Math.floor(Math.random()*1e9); cbs[id]=res; fn(id); }); }
  function pj(s){ try{ return JSON.parse(s); }catch(e){ return null; } }
  window.harmony = {
    isHarmony: true,
    app: n.appName(),
    getEnv: function(){ return pj(n.getEnv()); },
    getBattery: function(){ return pj(n.getBattery()); },
    getDeviceInfo: function(){ return pj(n.getDeviceInfo()); },
    toast: function(m){ n.toast(String(m)); },
    vibrate: function(ms){ n.vibrate(ms===undefined?50:ms); },
    setClipboard: function(t){ n.setClipboard(String(t)); },
    getClipboard: function(){ return ac(function(id){ n.getClipboard(id); }); },
    openInBrowser: function(u){ n.openInBrowser(String(u)); },
    share: function(t){ n.share(String(t)); },
    keepScreenOn: function(b){ n.keepScreenOn(!!b); },
    setBrightness: function(v){ n.setBrightness(v); },
    exit: function(){ n.exit(); },
    minimize: function(){ n.minimize(); },
    reload: function(){ n.reload(); },
    store: function(k,v){ n.store(String(k), String(v)); },
    load: function(k){ return ac(function(id){ n.load(String(k), id); }); },
    notify: function(t,b){ n.notify(String(t||''), String(b||'')); }
  };
  function Notif(title, opts){
    opts = opts || {};
    try{ n.notify(String(title||''), String(opts.body||'')); }catch(e){}
    return { close: function(){}, onclick:null, onerror:null, onshow:null, onclose:null };
  }
  Notif.permission = 'granted';
  Notif.requestPermission = function(cb){ if(typeof cb==='function'){ try{cb('granted');}catch(e){} } return Promise.resolve('granted'); };
  try{ window.Notification = Notif; }catch(e){}
  try{ document.dispatchEvent(new Event('harmonyready')); }catch(e){}
})();`;

class NativeBridge {
  ctx: common.UIAbilityContext;
  ctrl: webview.WebviewController;
  prefs: preferences.Preferences | null = null;
  private notifyId: number = 1;

  constructor(ctx: common.UIAbilityContext, ctrl: webview.WebviewController) {
    this.ctx = ctx;
    this.ctrl = ctrl;
    preferences.getPreferences(this.ctx, 'webapp_kv').then((p: preferences.Preferences) => { this.prefs = p; }).catch(() => {});
  }

  notify(title: string, body: string): void {
    const id: number = this.notifyId++;
    const req: notificationManager.NotificationRequest = {
      id: id,
      content: {
        notificationContentType: notificationManager.ContentType.NOTIFICATION_CONTENT_BASIC_TEXT,
        normal: { title: title || '', text: body || '', additionalText: '' }
      }
    };
    notificationManager.publish(req).catch(() => {
      try { notificationManager.requestEnableNotification(this.ctx).catch(() => {}); } catch (e) {}
    });
  }

  appName(): string { return APP_NAME; }

  getEnv(): string {
    return JSON.stringify({
      harmony: true, app: APP_NAME, bundleName: BUNDLE_NAME,
      brand: deviceInfo.brand, manufacturer: deviceInfo.manufacture,
      model: deviceInfo.productModel, deviceType: deviceInfo.deviceType,
      osFullName: deviceInfo.osFullName, sdkApiVersion: deviceInfo.sdkApiVersion
    });
  }

  getBattery(): string {
    return JSON.stringify({ soc: batteryInfo.batterySOC, chargingStatus: batteryInfo.chargingStatus });
  }

  getDeviceInfo(): string {
    return JSON.stringify({
      brand: deviceInfo.brand, manufacturer: deviceInfo.manufacture, marketName: deviceInfo.marketName,
      model: deviceInfo.productModel, deviceType: deviceInfo.deviceType,
      osFullName: deviceInfo.osFullName, sdkApiVersion: deviceInfo.sdkApiVersion
    });
  }

  toast(msg: string): void {
    try { promptAction.showToast({ message: msg }); } catch (e) {}
  }

  vibrate(ms: number): void {
    vibrator.startVibration({ type: 'time', duration: ms }, { id: 0, usage: 'notification' }).catch(() => {});
  }

  setClipboard(text: string): void {
    try {
      const d: pasteboard.PasteData = pasteboard.createData(pasteboard.MIMETYPE_TEXT_PLAIN, text);
      pasteboard.getSystemPasteboard().setData(d).catch(() => {});
    } catch (e) {}
  }

  getClipboard(reqId: string): void {
    pasteboard.getSystemPasteboard().getData().then((d: pasteboard.PasteData) => {
      let t: string = '';
      try {
        if (d.getRecordCount() > 0) {
          const rec: pasteboard.PasteDataRecord = d.getRecord(0);
          if (rec.plainText) { t = rec.plainText; }
        }
      } catch (e) {}
      if (!t) { try { const r: string = d.getPrimaryText(); if (typeof r === 'string') { t = r; } } catch (e) {} }
      this.resolve(reqId, t);
    }).catch(() => { this.resolve(reqId, ''); });
  }

  openInBrowser(url: string): void {
    this.ctx.startAbility({
      action: 'ohos.want.action.viewData',
      entities: ['entity.system.browsable'],
      uri: url
    }).catch(() => {});
  }

  share(text: string): void {
    try {
      const data: systemShare.SharedData = new systemShare.SharedData({
        utd: utd.UniformDataType.PLAIN_TEXT,
        content: text
      });
      const sc: systemShare.ShareController = new systemShare.ShareController(data);
      sc.show(this.ctx, { previewMode: systemShare.SharePreviewMode.DEFAULT, selectionMode: systemShare.SelectionMode.SINGLE }).catch(() => {});
    } catch (e) {}
  }

  keepScreenOn(on: boolean): void {
    window.getLastWindow(this.ctx).then((w: window.Window) => { w.setWindowKeepScreenOn(on); }).catch(() => {});
  }

  setBrightness(v: number): void {
    window.getLastWindow(this.ctx).then((w: window.Window) => { w.setWindowBrightness(v); }).catch(() => {});
  }

  exit(): void { this.ctx.terminateSelf(); }

  minimize(): void {
    window.getLastWindow(this.ctx).then((w: window.Window) => { w.minimize(); }).catch(() => {});
  }

  reload(): void { try { this.ctrl.refresh(); } catch (e) {} }

  store(key: string, value: string): void {
    if (this.prefs) {
      this.prefs.put(key, value).then(() => { if (this.prefs) { this.prefs.flush(); } }).catch(() => {});
    }
  }

  load(key: string, reqId: string): void {
    if (this.prefs) {
      this.prefs.get(key, '').then((v: preferences.ValueType) => { this.resolve(reqId, String(v)); }).catch(() => { this.resolve(reqId, ''); });
    } else {
      this.resolve(reqId, '');
    }
  }

  private resolve(reqId: string, val: string): void {
    const safe: string = (val === undefined || val === null) ? '' : val;
    const js: string = 'window.__hresolve(' + JSON.stringify(reqId) + ', ' + JSON.stringify(safe) + ')';
    try { this.ctrl.runJavaScript(js); } catch (e) {}
  }
}

@CustomDialog
struct PromptDialog {
  controller?: CustomDialogController;
  msg: string = '';
  defValue: string = '';
  value: string = '';
  onConfirm: (v: string) => void = () => {};
  onCancel: () => void = () => {};

  aboutToAppear(): void { this.value = this.defValue; }

  build() {
    Column({ space: 14 }) {
      Text(this.msg).fontSize(16).fontColor('#222222')
      TextInput({ text: this.value })
        .onChange((v: string) => { this.value = v; })
      Row({ space: 24 }) {
        Button('取消').backgroundColor('#cccccc').onClick(() => {
          const cb = this.onCancel;
          this.controller?.close();
          try { cb(); } catch (e) {}
        })
        Button('确定').onClick(() => {
          const v = this.value;
          const cb = this.onConfirm;
          this.controller?.close();
          try { cb(v); } catch (e) {}
        })
      }.margin({ top: 8 })
    }
    .padding(24).width('85%').backgroundColor(Color.White).borderRadius(16)
  }
}

@CustomDialog
struct AuthDialog {
  controller?: CustomDialogController;
  host: string = '';
  username: string = '';
  password: string = '';
  onConfirm: (u: string, p: string) => void = () => {};
  onCancel: () => void = () => {};

  build() {
    Column({ space: 14 }) {
      Text('需要登录').fontSize(18).fontWeight(FontWeight.Bold)
      Text(this.host).fontSize(13).fontColor('#888888')
      TextInput({ placeholder: '用户名' }).onChange((v: string) => { this.username = v; })
      TextInput({ placeholder: '密码' }).type(InputType.Password).onChange((v: string) => { this.password = v; })
      Row({ space: 24 }) {
        Button('取消').backgroundColor('#cccccc').onClick(() => { this.onCancel(); this.controller?.close(); })
        Button('登录').onClick(() => { this.onConfirm(this.username, this.password); this.controller?.close(); })
      }.margin({ top: 8 })
    }
    .padding(24).width('85%').backgroundColor(Color.White).borderRadius(16)
  }
}

@Entry
@Component
struct Index {
  controller: webview.WebviewController = new webview.WebviewController();
  @State private authHost: string = '';
  private downloadDelegate: webview.WebDownloadDelegate = new webview.WebDownloadDelegate();
  private context: common.UIAbilityContext = getContext(this) as common.UIAbilityContext;
  private pendingAuth: HttpAuthHandler | null = null;
  private bridge: NativeBridge = new NativeBridge(this.context, this.controller);

  private authDialog: CustomDialogController = new CustomDialogController({
    builder: AuthDialog({
      host: this.authHost,
      onConfirm: (u: string, p: string): void => { if (this.pendingAuth) { this.pendingAuth.confirm(u, p); } },
      onCancel: (): void => { if (this.pendingAuth) { this.pendingAuth.cancel(); } }
    }),
    autoCancel: false
  });

  @State private promptMsg: string = '';
  @State private promptDef: string = '';
  private pendingPromptOk: ((v: string) => void) | null = null;
  private pendingPromptCancel: (() => void) | null = null;
  private promptDialog: CustomDialogController = new CustomDialogController({
    builder: PromptDialog({
      msg: this.promptMsg,
      defValue: this.promptDef,
      onConfirm: (v: string): void => { if (this.pendingPromptOk) { this.pendingPromptOk(v); } },
      onCancel: (): void => { if (this.pendingPromptCancel) { this.pendingPromptCancel(); } }
    }),
    autoCancel: false
  });

  onBackPress(): boolean {
    try {
      this.controller.runJavaScript("(function(){var ev=new Event('harmonyback',{cancelable:true});var nc=window.dispatchEvent(ev);if(nc&&window.history&&window.history.length>1){window.history.back();}})()");
    } catch (e) {}
    return true;
  }

  aboutToAppear(): void {
    this.downloadDelegate.onBeforeDownload((item: webview.WebDownloadItem) => {
      let n: string = item.getSuggestedFileName();
      if (!n) { n = 'download_' + Date.now(); }
      item.start(this.context.filesDir + '/' + n);
      promptAction.showToast({ message: '开始下载：' + n });
    });
    this.downloadDelegate.onDownloadFinish((item: webview.WebDownloadItem) => {
      const full: string = item.getFullPath();
      promptAction.showToast({ message: '下载完成：' + full });
      this.openFile(full);
    });
    this.downloadDelegate.onDownloadFailed((item: webview.WebDownloadItem) => {
      promptAction.showToast({ message: '下载失败' });
    });
  }

  private openFile(path: string): void {
    try {
      const uri: string = fileUri.getUriFromPath(path);
      this.context.startAbility({ action: 'ohos.want.action.viewData', uri: uri, flags: wantConstant.Flags.FLAG_AUTH_READ_URI_PERMISSION });
    } catch (e) { }
  }

  build() {
    Column() {
      Web({ src: 'https://mail.hwaipy.cn/', controller: this.controller })
        .width('100%').height('100%')
        .javaScriptAccess(true).domStorageAccess(true).databaseAccess(true)
        .imageAccess(true).onlineImageAccess(true)
        .mixedMode(MixedMode.All).cacheMode(CacheMode.Default).geolocationAccess(true)
        .darkMode(WebDarkMode.Auto)
        .javaScriptOnDocumentStart([{ script: HARMONY_SHIM, scriptRules: ['*'] }])
        .onControllerAttached(() => {
          this.controller.setDownloadDelegate(this.downloadDelegate);
          try {
            this.controller.registerJavaScriptProxy(this.bridge, '__hbridge', [
              'appName', 'getEnv', 'getBattery', 'getDeviceInfo', 'toast', 'vibrate',
              'setClipboard', 'getClipboard', 'openInBrowser', 'share', 'keepScreenOn',
              'setBrightness', 'exit', 'minimize', 'reload', 'store', 'load', 'notify'
            ]);
          } catch (e) { }
        })
        .onHttpAuthRequest((event) => {
          if (event.handler.isHttpAuthInfoSaved()) { return true; }
          this.authHost = event.host;
          this.pendingAuth = event.handler;
          this.authDialog.open();
          return true;
        })
        .multiWindowAccess(true)
        .onWindowNew((event) => {
          const u: string = event.targetUrl;
          if (u && (u.startsWith('http://') || u.startsWith('https://'))) {
            this.bridge.openInBrowser(u);
          }
          event.handler.setWebController(null);
        })
        .onAlert((event) => {
          promptAction.showDialog({
            title: '提示',
            message: event.message,
            buttons: [{ text: '确定', color: '#1f6feb' }]
          }).then(() => { event.result.handleConfirm(); }).catch(() => { event.result.handleConfirm(); });
          return true;
        })
        .onConfirm((event) => {
          promptAction.showDialog({
            title: '提示',
            message: event.message,
            buttons: [
              { text: '取消', color: '#666666' },
              { text: '确定', color: '#1f6feb' }
            ]
          }).then((r) => {
            if (r.index === 1) { event.result.handleConfirm(); } else { event.result.handleCancel(); }
          }).catch(() => { event.result.handleCancel(); });
          return true;
        })
        .onPrompt((event) => {
          this.promptMsg = event.message;
          this.promptDef = event.value;
          this.pendingPromptOk = (v: string): void => { event.result.handlePromptConfirm(v); };
          this.pendingPromptCancel = (): void => { event.result.handleCancel(); };
          this.promptDialog.open();
          return true;
        })
        .onBeforeUnload((event) => {
          promptAction.showDialog({
            title: '离开页面?',
            message: event.message,
            buttons: [
              { text: '取消', color: '#666666' },
              { text: '离开', color: '#e85c41' }
            ]
          }).then((r) => {
            if (r.index === 1) { event.result.handleConfirm(); } else { event.result.handleCancel(); }
          }).catch(() => { event.result.handleCancel(); });
          return true;
        })
    }
    .width('100%').height('100%')
  }
}
