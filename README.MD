# DeepSeek2API

[![](https://img.shields.io/github/license/iidamie/deepseek2api.svg)](LICENSE)
![](https://img.shields.io/github/stars/iidamie/deepseek2api.svg)
![](https://img.shields.io/github/forks/iidamie/deepseek2api.svg)

main branch : https://github.com/iidamie/deepseek2api

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

不自帶comfig.json ，格式一樣 直接複製就好，

更新log：
完全捨棄claude code（難道你手機也會用claude嗎？）
完全捨棄整個舊結構
優化 縮短代碼結構
解決FINISHED問題（現在不知道還有沒有？）
新增輪替功能