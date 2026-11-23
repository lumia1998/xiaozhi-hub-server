# Hub 管理前端

在此目录执行 `npm ci && npm run build`，更新同级 `static/console-ui.js` 和 `static/console-ui.css`。生成文件随 Python 服务分发，不需要浏览器联网下载依赖。

React 19 + [Astryx](https://github.com/meta/astryx) 管理登录、导航、明暗主题、状态看板及音箱预览与布局选择。既有设置页继续使用原有渲染器，保留 hash 路由、未保存修改检查和管理 API。预览 iframe 加载客户端自己的构建，请与客户端 `web/` 资源配套使用。
