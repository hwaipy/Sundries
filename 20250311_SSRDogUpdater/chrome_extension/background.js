chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.type === 'FETCH_EXTERNAL_API') {
    fetch('https://fly.hwaipy.cn/update', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: request.payload
    }).then(response => {
      if (response.ok) {
        sendResponse({ success: true })
      } else {
        sendResponse({ success: false, error: `Response code: ${response.status}` });
      }
    }).catch(error => {
      sendResponse({ success: false, error: error.message });
    });
    return true;
  }
});