# DeepSeek2API

[![](https://img.shields.io/github/license/iidamie/deepseek2api.svg)](LICENSE)
![](https://img.shields.io/github/stars/iidamie/deepseek2api.svg)
![](https://img.shields.io/github/forks/iidamie/deepseek2api.svg)

main branch : https://github.com/iidamie/deepseek2api

# 免责声明
## 逆向API是不稳定的，建议前往DeepSeek官方https://platform.deepseek.com/ 付费使用API，避免封禁的风险。

### 本组织和个人不接受任何资金捐助和交易，此项目是纯粹研究交流学习性质！

### 仅限自用，禁止对外提供服务或商用，避免对官方造成服务压力，否则风险自担！

### 仅限自用，禁止对外提供服务或商用，避免对官方造成服务压力，否则风险自担！

### 仅限自用，禁止对外提供服务或商用，避免对官方造成服务压力，否则风险自担！



本版本做出改變:
```
cffl url -> reuqests
transformer -> X
wasmtime -> wasm3
```
全是為termux做的適配 

我沒搞docker,啥的更新 嗯 因為我不會 

項目由deepseek v3.2大力支持~  自己破解自己（不是

98%都deepseek寫的

#### v1/models
```json
{
  "object": "list",
  "data": [
    {
      "id": "deepseek-chat",
      "object": "model",
      "created": 1700000000,
      "owned_by": "deepseek"
    },
    {
      "id": "deepseek-reasoner",
      "object": "model",
      "created": 1700000000,
      "owned_by": "deepseek"
    },
    {
      "id": "deepseek-v4-flash",
      "object": "model",
      "created": 1700000000,
      "owned_by": "deepseek"
    },
    {
      "id": "deepseek-v4-pro",
      "object": "model",
      "created": 1700000000,
      "owned_by": "deepseek"
    },
    {
      "id": "deepseek-vision",
      "object": "model",
      "created": 1700000000,
      "owned_by": "deepseek"
    },
    {
      "id": "deepseek-v4-flash-searching",
      "object": "model",
      "created": 1700000000,
      "owned_by": "deepseek"
    },
    {
      "id": "deepseek-v4-pro-searching",
      "object": "model",
      "created": 1700000000,
      "owned_by": "deepseek"
    },
    {
      "id": "deepseek-vision-searching",
      "object": "model",
      "created": 1700000000,
      "owned_by": "deepseek"
    }
  ]
}
```
> 使用thinking請傳入`{"thinking": {"type": "enabled/disabled"}}`


#### 更新log 1.1：
```
適配全模型
修復非流式
辨別並修復thinking_content格式
適配deepseek官方api格式
```

#### 更新log 1.0：
```
完全捨棄claude code（難道你手機也會用claude嗎？）
完全捨棄整個舊結構
優化 縮短代碼結構
解決FINISHED問題（現在不知道還有沒有？）
新增帳號輪替使用功能
支持expert模型（默認使用 用快速要自己改代碼）
```