import requests
from qcloud_cos import CosConfig
from qcloud_cos import CosS3Client

secret_id = '-'      
secret_key = '-'    
region = 'ap-shanghai'
bucket = 'flysub-1311552035'

def update(url):
  headers = { "User-Agent": "ClashX Meta/v1.4.30 (com.metacubex.ClashX.meta; build:638; macOS 26.3.1) Alamofire/5.10.2" }
    
  try:
    response = requests.get(url, headers=headers, timeout=10)

    if response.status_code == 200:
      content = response.text
    else:
      print("错误响应:", response.text)
      return False
  except Exception as e:
    print(f"发生异常: {e}")
    return False

  config = CosConfig(Region=region, SecretId=secret_id, SecretKey=secret_key)
  client = CosS3Client(config)
  try:
    response = client.put_object(
        Bucket=bucket,
        Body=content.encode('utf-8'),
        Key='config.txt',
        ContentType='text/plain; charset=utf-8',
        CacheControl='no-cache'
    )
    return True
  except Exception as e:
    print(f"❌ 上传失败: {str(e)}")
    return False

# url = "https://a9a1ce2bec06a447bb2aa3697be0cb70.m4hs-9ekt-y2qb-fa7x-wl6r.sbs/s?q=28f8a594&t=a9a1ce2bec06a447bb2aa3697be0cb70.jpg"
# update(url)

from http.server import HTTPServer, BaseHTTPRequestHandler

class MyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length).decode('utf-8')

        print("📥 Received POST data:")        
        print(post_data)
        success = False
        try:
            url = post_data.strip()
            if update(url):
                success = True
                print("✅ Update successful!")
            else:
                print("❌ Update failed.")
        except Exception as e:
            print(f"⚠️ Error processing POST data: {e}")

        self.send_response(200 if success else 500)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"OK!" if success else b"Failed!")

def run_server(port):
    server_address = ('', port)
    httpd = HTTPServer(server_address, MyHandler)
    print(f"🚀 Server started at http://localhost:{port}")
    httpd.serve_forever()

if __name__ == "__main__":
    run_server(25789)
