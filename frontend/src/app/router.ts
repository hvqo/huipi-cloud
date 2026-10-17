import { createRouter, createWebHistory } from 'vue-router'
import { DEMO_MODE } from './mode'
import { initSession, session, acceptOidcSession, refreshSession } from './session'
import { identity, callback, safeReturn } from '../auth'
import Login from '../views/Login.vue'
import WorkbenchHome from '../views/teacher/WorkbenchHome.vue'
import Assignments from '../views/teacher/Assignments.vue'
import AssignmentDetail from '../views/teacher/AssignmentDetail.vue'
import QuestionEdit from '../views/teacher/QuestionEdit.vue'
import SubmissionDetail from '../views/teacher/SubmissionDetail.vue'
import AnswerReview from '../views/teacher/AnswerReview.vue'
import GradeReview from '../views/teacher/GradeReview.vue'
import Assistant from '../views/teacher/Assistant.vue'
import Knowledge from '../views/teacher/Knowledge.vue'
import Insights from '../views/teacher/Insights.vue'
import Settings from '../views/teacher/Settings.vue'
import AdminHome from '../views/admin/AdminHome.vue'
import AdminTeachers from '../views/admin/AdminTeachers.vue'
import AdminAuthorizations from '../views/admin/AdminAuthorizations.vue'
import AdminAudit from '../views/admin/AdminAudit.vue'
import AdminSystem from '../views/admin/AdminSystem.vue'
import Forbidden from '../views/Forbidden.vue'
import NotFound from '../views/NotFound.vue'

/**
 * 路由与守卫：/me 角色决定入口；管理员路由仅 admin 可进；
 * 一切权限仍由服务端逐请求校验，前端守卫只做导航体验。
 */

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: Login, meta: { public: true, breadcrumb: ['登录'] } },
    {
      path: '/auth/callback',
      component: Login,
      meta: { public: true },
      beforeEnter: async () => {
        if (DEMO_MODE) return '/login'
        try {
          const target = await callback()
          await acceptOidcSession()
          return target === '/' ? (session.role === 'admin' ? '/admin' : '/workbench') : target
        } catch {
          identity.error = '登录确认未完成，请重新登录。'
          return '/login'
        }
      },
    },
    { path: '/', redirect: () => (session.role === 'admin' ? '/admin' : '/workbench') },
    { path: '/workbench', component: WorkbenchHome, meta: { breadcrumb: ['工作台'] } },
    {
      path: '/assignments',
      component: Assignments,
      meta: { breadcrumb: ['我的作业'] },
    },
    {
      path: '/assignments/:aid',
      component: AssignmentDetail,
      meta: { breadcrumb: ['我的作业', '作业详情'] },
    },
    {
      path: '/assignments/:aid/questions/:qid',
      component: QuestionEdit,
      meta: { breadcrumb: ['我的作业', '题目编辑'] },
    },
    {
      path: '/assignments/:aid/submissions/:sid',
      component: SubmissionDetail,
      meta: { breadcrumb: ['我的作业', '提交详情'] },
    },
    {
      path: '/assignments/:aid/submissions/:sid/questions/:qid/review',
      component: AnswerReview,
      meta: { breadcrumb: ['我的作业', '作答核对'] },
    },
    {
      path: '/assignments/:aid/submissions/:sid/questions/:qid/grading',
      component: GradeReview,
      meta: { breadcrumb: ['我的作业', 'AI 候选评分审核'] },
    },
    { path: '/assistant', component: Assistant, meta: { breadcrumb: ['AI 助教'] } },
    { path: '/knowledge', component: Knowledge, meta: { breadcrumb: ['知识库'] } },
    { path: '/insights', component: Insights, meta: { breadcrumb: ['教学统计'] } },
    { path: '/settings', component: Settings, meta: { breadcrumb: ['设置'] } },
    {
      path: '/admin',
      component: AdminHome,
      meta: { admin: true, breadcrumb: ['管理', '管理概览'] },
    },
    {
      path: '/admin/teachers',
      component: AdminTeachers,
      meta: { admin: true, breadcrumb: ['管理', '教师账号'] },
    },
    {
      path: '/admin/authorizations',
      component: AdminAuthorizations,
      meta: { admin: true, breadcrumb: ['管理', '作业授权'] },
    },
    {
      path: '/admin/audit',
      component: AdminAudit,
      meta: { admin: true, breadcrumb: ['管理', '操作审计'] },
    },
    {
      path: '/admin/system',
      component: AdminSystem,
      meta: { admin: true, breadcrumb: ['管理', '系统状态'] },
    },
    { path: '/403', component: Forbidden, meta: { breadcrumb: ['无权限'] } },
    { path: '/:pathMatch(.*)*', component: NotFound, meta: { breadcrumb: ['未找到'] } },
  ],
})

router.beforeEach(async (to) => {
  if (!session.ready) await initSession()
  if (to.meta.public) {
    if (session.authenticated && to.path === '/login') {
      return session.role === 'admin' ? '/admin' : '/workbench'
    }
    return true
  }
  if (!session.authenticated) {
    return { path: '/login', query: { returnTo: safeReturn(to.query.returnTo ?? to.fullPath) } }
  }
  if (to.meta.admin) await refreshSession()
  if (to.meta.admin && session.role !== 'admin') return '/403'
  return true
})
