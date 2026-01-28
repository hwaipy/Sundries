from openai import OpenAI

client = OpenAI(api_key="EMPTY", base_url="https://glm.qpqi.group/v1/")

# def simple_chat(use_stream=True):
#     messages = [
#         {
#             "role": "system",
#             "content": "You are ChatGLM3, a large language model trained by Zhipu.AI. Follow the user's "
#                        "instructions carefully. Respond using markdown.",
#         },
#         {
#             "role": "user",
#             "content": "你好，请你用生动的话语给我讲一个小故事吧"
#         }
#     ]
#     response = client.chat.completions.create(
#         model="chatglm3-6b",
#         messages=messages,
#         stream=use_stream,
#         max_tokens=256,
#         temperature=0.8,
#         presence_penalty=1.1,
#         top_p=0.8)
#     if response:
#         if use_stream:
#             for chunk in response:
#                 print(chunk.choices[0].delta.content)
#         else:
#             content = response.choices[0].message.content
#             print(content)
#     else:
#         print("Error:", response.status_code)

def simpleQA(question):
  messages = [
    {
        "role": "system",
        "content": "You are ChatGLM3, a large language model trained by Zhipu.AI. Follow the user's "
                    "instructions carefully. Respond using markdown.",
    },
    {
        "role": "user",
        "content": question
    }
  ]
  response = client.chat.completions.create(model="chatglm3-6b", messages=messages, stream=False, max_tokens=256, temperature=0.8, presence_penalty=1.1, top_p=0.8)
  if response:
    return response.choices[0].message.content
  else:
    raise ValueError("Error:", response.status_code)


if __name__ == "__main__":
    respopnse = simpleQA("请给我讲一个十个字以内的小故事")
    
    print(respopnse)