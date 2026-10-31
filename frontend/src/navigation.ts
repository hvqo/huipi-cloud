import { reactive } from 'vue'
import { configuration } from './api/exam-configuration'
import { examResults } from './api/exam-results'
import { marking } from './api/marking'
import { papers } from './api/papers'
import { recovery } from './api/recovery'
import { scans } from './api/scans'
import { identity } from './auth'

// Menu visibility is a convenience only. Every business request is still authorized by the server.
export const navigationAccess = reactive({
  papers: false,
  exams: false,
  scans: false,
  omr: false,
  markingManagement: false,
  myMarking: false,
  exceptions: false,
  results: false,
})
let generation = 0
let active: { actor: string; promise: Promise<void> } | undefined
let lastLoaded = 0
export function clearNavigation() {
  generation++
  active = undefined
  lastLoaded = 0
  for (const key of Object.keys(navigationAccess) as (keyof typeof navigationAccess)[])
    navigationAccess[key] = false
}
export async function loadNavigation(actor: string) {
  if (!actor || identity.actor !== actor) return
  if (active?.actor === actor) return active.promise
  // A short UI-only throttle avoids issuing ten duplicate queries during rapid route changes.
  // The API independently checks the current grants on every business operation.
  if (Date.now() - lastLoaded < 5000) return
  const current = ++generation
  const set = (key: keyof typeof navigationAccess, value: boolean) => {
    if (identity.actor === actor && current === generation) navigationAccess[key] = value
  }
  const scan = scans.sessions()
  const choices = configuration.papers()
  const task = Promise.allSettled([
    papers
      .list()
      .then((v) => set('papers', v.can_create || v.items.length > 0))
      .catch(() => set('papers', false)),
    Promise.allSettled([
      configuration.options(),
      configuration.events(),
      configuration.sessions(),
    ]).then(([o, e, s]) =>
      set(
        'exams',
        (o.status === 'fulfilled' && o.value.can_create_event) ||
          (e.status === 'fulfilled' && e.value.items.length > 0) ||
          (s.status === 'fulfilled' && s.value.items.length > 0),
      ),
    ),
    scan.then((v) => set('scans', v.items.length > 0)).catch(() => set('scans', false)),
    Promise.allSettled([scan, choices]).then(([s, p]) =>
      set(
        'omr',
        (s.status === 'fulfilled' && s.value.items.length > 0) ||
          (p.status === 'fulfilled' && p.value.items.length > 0),
      ),
    ),
    marking
      .sessions()
      .then((v) => set('markingManagement', v.items.length > 0))
      .catch(() => set('markingManagement', false)),
    marking
      .mine()
      .then((v) => set('myMarking', v.items.length > 0 || v.counts.some((item) => item.count > 0)))
      .catch(() => set('myMarking', false)),
    recovery
      .sessions()
      .then((v) => set('exceptions', v.items.length > 0))
      .catch(() => set('exceptions', false)),
    examResults
      .sessions()
      .then((v) => set('results', v.items.length > 0))
      .catch(() => set('results', false)),
  ]).then(() => {
    if (identity.actor === actor && current === generation) lastLoaded = Date.now()
  })
  active = { actor, promise: task }
  try {
    await task
  } finally {
    if (active?.promise === task) active = undefined
  }
}
