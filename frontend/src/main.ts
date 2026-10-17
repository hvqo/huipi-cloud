import { createApp } from 'vue'
import { ElButton, ElDialog, ElInput, ElSelect, ElOption } from 'element-plus'
import 'element-plus/dist/index.css'
import './style.css'
import App from './App.vue'
import { router } from './app/router'

const app = createApp(App).use(router)
for (const component of [ElButton, ElDialog, ElInput, ElSelect, ElOption])
  app.component(component.name!, component)
app.mount('#app')
