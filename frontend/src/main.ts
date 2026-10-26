import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import { ElButton, ElDialog, ElInput, ElSelect, ElOption } from 'element-plus'
import 'element-plus/dist/index.css'
import './style.css'
import App from './App.vue'
import Login from './views/Login.vue'
import Assignments from './views/Assignments.vue'
import Assignment from './views/Assignment.vue'
import Submission from './views/Submission.vue'
import Review from './views/Review.vue'
import ScanIntake from './views/ScanIntake.vue'
import AnswerScripts from './views/AnswerScripts.vue'
import ExamResults from './views/ExamResults.vue'
import ExamConfiguration from './views/ExamConfiguration.vue'
import Papers from './views/Papers.vue'
import School from './views/School.vue'
import MarkingManagement from './views/MarkingManagement.vue'
import MyMarkingTasks from './views/MyMarkingTasks.vue'
import { initializeAuth, identity, callback } from './auth'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: Login },
    {
      path: '/auth/callback',
      component: Login,
      beforeEnter: async () => {
        try {
          return await callback()
        } catch {
          identity.error = '登录确认未完成，请重新登录。'
          return '/login'
        }
      },
    },
    { path: '/', redirect: '/assignments' },
    { path: '/assignments', component: Assignments },
    { path: '/scans', component: ScanIntake },
    { path: '/exam-results', component: ExamResults },
    { path: '/exams', component: ExamConfiguration },
    { path: '/school', component: School },
    { path: '/marking/manage', component: MarkingManagement },
    { path: '/marking/progress', component: MarkingManagement, props: { progressOnly: true } },
    { path: '/marking/my', component: MyMarkingTasks },
    { path: '/marking/my/:tid', component: MyMarkingTasks },
    { path: '/papers', component: Papers },
    { path: '/papers/:pid', component: Papers },
    { path: '/exams/sessions/:sid', component: ExamConfiguration },
    { path: '/scans/sessions/:sid/results', component: ExamResults },
    { path: '/scans/sessions/:sid/scripts', component: AnswerScripts },
    { path: '/assignments/:aid', component: Assignment },
    { path: '/assignments/:aid/submissions/:sid', component: Submission },
    { path: '/assignments/:aid/submissions/:sid/questions/:qid/review', component: Review },
    { path: '/:pathMatch(.*)*', redirect: '/assignments' },
  ],
})
router.beforeEach(async (to) => {
  if (!identity.ready) await initializeAuth()
  if (!['/login', '/auth/callback'].includes(to.path) && !identity.authenticated)
    return { path: '/login', query: { returnTo: to.fullPath } }
})
const app = createApp(App).use(router)
for (const component of [ElButton, ElDialog, ElInput, ElSelect, ElOption])
  app.component(component.name!, component)
app.mount('#app')
