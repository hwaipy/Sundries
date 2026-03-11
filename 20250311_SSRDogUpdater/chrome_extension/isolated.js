window.addEventListener("message", (event) => {
  if (event.data && event.data.type === 'SSRDOG_BRIDGE_MSG') {
    chrome.runtime.sendMessage({
      type: 'FETCH_EXTERNAL_API',
      payload: event.data.payload
    }, (response) => {
      window.postMessage({
        type: 'SSRDOG_BRIDGE_MSG_RESPONSE',
        payload: response
      }, "*");
    });
  }
}, false);