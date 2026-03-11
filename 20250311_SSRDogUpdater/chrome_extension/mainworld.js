(function () {
  // --- 1. 现代 API 劫持 ---
  if (navigator.clipboard) {
    const originalWriteText = navigator.clipboard.writeText;
    navigator.clipboard.writeText = async function (text) {
      handleCapturedData(text, 'writeText');
      return originalWriteText.apply(this, arguments);
    };
  }

  // --- 2. 针对 execCommand 的“无警告”劫持 ---
  const originalExec = document.execCommand;
  Object.defineProperty(document, 'execCommand', {
    value: function (command, showUI, value) {
      if (command.toLowerCase() === 'copy') {
        // 延迟一丁点时间读取，确保内容已进入剪贴板或已被选中
        const text = window.getSelection().toString() ||
          (document.activeElement ? (document.activeElement.value || document.activeElement.innerText) : "");

        if (text) {
          handleCapturedData(text, 'execCommand');
        }
      }
      return originalExec.apply(this, arguments);
    },
    configurable: true,
    writable: true
  });

  // --- 3. 终极补丁：监听系统级 copy 事件 ---
  document.addEventListener('copy', () => {
    setTimeout(async () => {
      const text = window.getSelection().toString();
      console.log(text);

      if (text) handleCapturedData(text, 'DOM_Copy_Event');
    }, 50);
  }, true); // 使用捕获阶段

  function handleCapturedData(text, source) {
    console.log(`%c[捕获成功][${source}]`, 'color: #4CAF50; font-weight: bold;', text);
    // 这里执行你的业务逻辑

    window.postMessage({ 
      type: 'SSRDOG_BRIDGE_MSG', 
      payload: text 
    }, "*");
  }
})();

window.addEventListener("message", (event) => {
  if (event.data && event.data.type === 'SSRDOG_BRIDGE_MSG_RESPONSE') {
    const result = event.data.payload
    success = result.success
    message = success ? "OK" : result.error || "Unknown Error"

    const toast = document.createElement('div');
    toast.style.cssText = 'position:fixed;top:10px;left:10px;background:#' + (success ? "00C853" : "FF5252") + ';color:white;padding:8px 12px;z-index:1000000;border-radius:4px;font-family:sans-serif;box-shadow:0 2px 10px rgba(0,0,0,0.2);';
    toast.innerText = success ? "Updated" : "Failed: " + message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
  }
}, false);