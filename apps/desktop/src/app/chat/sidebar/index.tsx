import {
  closestCenter,
  DndContext,
  type DragEndEvent,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy
} from '@dnd-kit/sortable'
import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { PlatformAvatar } from '@/app/messaging/platform-icon'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { KbdGroup } from '@/components/ui/kbd'
import { SearchField } from '@/components/ui/search-field'
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem
} from '@/components/ui/sidebar'
import { Skeleton } from '@/components/ui/skeleton'
import type { HermesGitWorktree } from '@/global'
import { searchSessions, type SessionInfo, type SessionSearchResult } from '@/hermes'
import { useI18n } from '@/i18n'
import { comboTokens } from '@/lib/keybinds/combo'
import { profileColor } from '@/lib/profile-color'
import { sessionMatchesSearch } from '@/lib/session-search'
import { normalizeSessionSource, sessionSourceLabel } from '@/lib/session-source'
import { cn } from '@/lib/utils'
import { $cronJobs } from '@/store/cron'
import {
  $dismissedAutoProjectIds,
  $dismissedWorktreeIds,
  $panesFlipped,
  $pinnedSessionIds,
  $sidebarAgentsGrouped,
  $sidebarCronOpen,
  $sidebarMessagingOpenIds,
  $sidebarOpen,
  $sidebarOverlayMounted,
  $sidebarPinsOpen,
  $sidebarRecentsOpen,
  $sidebarSessionOrderIds,
  $sidebarSessionOrderManual,
  $sidebarWorkspaceCollapsedIds,
  $sidebarWorkspaceOrderIds,
  $sidebarWorkspaceParentOrderIds,
  dismissAutoProject,
  dismissWorktree,
  pinSession,
  SESSION_SEARCH_FOCUS_EVENT,
  setPinnedSessionOrder,
  setSidebarAgentsGrouped,
  setSidebarCronOpen,
  setSidebarPinsOpen,
  setSidebarRecentsOpen,
  setSidebarSessionOrderIds,
  setSidebarSessionOrderManual,
  setSidebarWorkspaceOrderIds,
  setSidebarWorkspaceParentOrderIds,
  SIDEBAR_SESSIONS_PAGE_SIZE,
  toggleSidebarMessagingOpen,
  toggleWorkspaceNodeCollapsed,
  unpinSession
} from '@/store/layout'
import { notifyError } from '@/store/notifications'
import {
  $newChatProfile,
  $profiles,
  $profileScope,
  ALL_PROFILES,
  newSessionInProfile,
  normalizeProfileKey
} from '@/store/profile'
import {
  $activeProjectId,
  $projects,
  $projectScope,
  $projectTree,
  $projectTreeLoading,
  $reposScanning,
  $scopedSessionIds,
  $worktreeRefreshToken,
  ALL_PROJECTS,
  copyPath,
  deleteProject,
  enterProject,
  exitProjectScope,
  fetchProjectSessions,
  openProjectAddFolder,
  openProjectCreate,
  openProjectRename,
  refreshProjects,
  refreshProjectTree,
  removeWorktreePath,
  revealPath,
  scanAndRecordRepos,
  setActiveProject,
  startWorkInRepo
} from '@/store/projects'
import {
  $cronSessions,
  $currentCwd,
  $gatewayState,
  $messagingPlatformTotals,
  $messagingSessions,
  $messagingTruncated,
  $selectedStoredSessionId,
  $sessionProfileTotals,
  $sessions,
  $sessionsLoading,
  $sessionsTotal,
  $workingSessionIds,
  sessionPinId,
  setCurrentCwd
} from '@/store/session'

import { type AppView, ARTIFACTS_ROUTE, MESSAGING_ROUTE, SKILLS_ROUTE } from '../../routes'
import { SidebarPanelLabel } from '../../shell/sidebar-label'
import type { SidebarNavItem } from '../../types'

import { SidebarCronJobsSection } from './cron-jobs-section'
import { SidebarLoadMoreRow } from './load-more-row'
import { reconcileFreshFirst, resolveManualSessionOrderIds } from './order'
import { ProfileRail } from './profile-switcher'
import { ProjectDialog } from './project-dialog'
import { SidebarSessionRow } from './session-row'
import { VirtualSessionList } from './virtual-session-list'
import {
  mergeRepoWorktreeGroups,
  overlayLiveLanes,
  overlayLivePreviews,
  sessionRecency as sessionTime,
  type SidebarProjectTree,
  type SidebarSessionGroup,
  type SidebarWorkspaceTree
} from './workspace-groups'

const VIRTUALIZE_THRESHOLD = 25

// Non-session groups (messaging platforms) stay compact: show a few rows up
// front, reveal more in larger steps on demand. Keeps a busy platform from
// dominating the sidebar before the user asks to see it.
const NON_SESSION_INITIAL_ROWS = 3
const NON_SESSION_LOAD_STEP = 10

const NEW_SESSION_KBD = comboTokens('mod+n')

const SIDEBAR_NAV: SidebarNavItem[] = [
  {
    id: 'new-session',
    label: '',
    icon: props => <Codicon name="robot" {...props} />,
    action: 'new-session'
  },
  {
    id: 'skills',
    label: '',
    icon: props => <Codicon name="symbol-misc" {...props} />,
    route: SKILLS_ROUTE
  },
  { id: 'messaging', label: '', icon: props => <Codicon name="comment" {...props} />, route: MESSAGING_ROUTE },
  { id: 'artifacts', label: '', icon: props => <Codicon name="files" {...props} />, route: ARTIFACTS_ROUTE }
]

const SIDEBAR_GROUP_PAGE = 5
// Two modes via the `compact` height variant (styles.css):
//   tall    → each section is shrink-0, capped, its own scroller; Sessions is flex-1.
//   compact → COMPACT_FLAT drops the caps so the whole stack scrolls as one.
// Sections stay shrink-0 so none can be squeezed below its content and bleed onto
// the next — the flexbox `min-height: auto` overlap trap that caused the bug.
const COMPACT_FLAT = 'compact:max-h-none compact:overflow-visible'

// Vertical scroll only — never a horizontal bar from glow bleed, long titles, etc.
const SCROLL_Y = 'overflow-y-auto overflow-x-hidden overscroll-contain'

// A non-session group's scroll body: own scroller when tall, flattened when compact.
const GROUP_BODY = cn(SCROLL_Y, COMPACT_FLAT)

// Section-header action icons stay hidden until the whole header row is hovered
// (group/section lives on SidebarSectionHeader), mirroring the artifacts/file
// browser header affordances. focus-visible keeps them keyboard-reachable.
const HEADER_ACTION_BTN =
  'text-(--ui-text-tertiary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground group-hover/section:opacity-100 focus-visible:opacity-100'

// The view toggle (overview group toggle / in-project back) is the one control
// that stays visible at all times — it's the stable navigation affordance, not
// a hover-revealed action.
const HEADER_NAV_BTN =
  'text-(--ui-text-tertiary) opacity-70 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground hover:opacity-100 focus-visible:opacity-100'

// Sidebar reordering is a strictly vertical list. The dragged item's transform
// is rendered Y-only in useSortableBindings (no x, no scale); this just stops
// dnd-kit's auto-scroll from dragging the rail — or the window — sideways when
// the pointer nears an edge, killing the horizontal "drag to valhalla".
const reorderAutoScroll = { threshold: { x: 0, y: 0.2 } }

// One self-contained, nesting-safe reorderable list. It owns its DndContext, so a
// drag only ever collides with THIS list's own items — drop it at any depth (repos,
// worktrees, sessions) and reordering "just works" without leaking into the lists
// around or inside it. Pair each item with useSortableBindings(id); the list reports
// the new id order and the caller persists it. This is the single generic primitive
// behind every reorderable surface in the sidebar.
function ReorderableList({
  children,
  ids,
  onReorder,
  sensors
}: {
  children: React.ReactNode
  ids: string[]
  onReorder: (ids: string[]) => void
  sensors?: ReturnType<typeof useSensors>
}) {
  const handleDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) {
      return
    }

    const from = ids.indexOf(String(active.id))
    const to = ids.indexOf(String(over.id))

    if (from >= 0 && to >= 0) {
      onReorder(arrayMove(ids, from, to))
    }
  }

  return (
    <DndContext autoScroll={reorderAutoScroll} collisionDetection={closestCenter} onDragEnd={handleDragEnd} sensors={sensors}>
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        {children}
      </SortableContext>
    </DndContext>
  )
}

const countLabel = (loaded: number, total: number) => (total > loaded ? `${loaded}/${total}` : String(loaded))

const pathListKey = (paths: string[]) =>
  paths.map(path => path.trim()).filter(Boolean).sort((a, b) => a.localeCompare(b)).join('\n')

// minmax(0,1fr): pin the single column to the rail width so long labels truncate
// instead of shoving controls off-screen.
const SIDEBAR_STACK = 'grid grid-cols-[minmax(0,1fr)] gap-px'

// Every session in a project, across its repos/worktrees (order-agnostic).
const projectSessions = (project: SidebarProjectTree): SessionInfo[] =>
  project.repos.flatMap(repo => repo.groups.flatMap(group => group.sessions))

const projectTreeCwd = (project: SidebarProjectTree): null | string =>
  project.path || project.repos.find(repo => repo.path)?.path || null

// Overview rows carry their activity stamp from the backend (lanes are empty in
// overview mode), falling back to loaded session times when present.
const projectActivityTime = (project: SidebarProjectTree): number =>
  Math.max(
    project.lastActive ?? 0,
    projectSessions(project).reduce((latest, s) => Math.max(latest, sessionTime(s)), 0)
  )

// The project's most-recent sessions, for the overview preview under each row.
const latestProjectSessions = (project: SidebarProjectTree, limit: number): SessionInfo[] =>
  [...projectSessions(project)].sort((a, b) => sessionTime(b) - sessionTime(a)).slice(0, limit)

function sortProjectsForOverview(projects: SidebarProjectTree[], activeProjectId: null | string): SidebarProjectTree[] {
  return [...projects].sort((a, b) => {
    const aActive = Boolean(activeProjectId && a.id === activeProjectId && !a.isAuto)
    const bActive = Boolean(activeProjectId && b.id === activeProjectId && !b.isAuto)

    if (aActive !== bActive) {
      return aActive ? -1 : 1
    }

    const aExplicit = !a.isAuto
    const bExplicit = !b.isAuto

    if (aExplicit !== bExplicit) {
      return aExplicit ? -1 : 1
    }

    const aHasSessions = a.sessionCount > 0
    const bHasSessions = b.sessionCount > 0

    if (aHasSessions !== bHasSessions) {
      return aHasSessions ? -1 : 1
    }

    const activityDelta = projectActivityTime(b) - projectActivityTime(a)

    if (activityDelta !== 0) {
      return activityDelta
    }

    return a.label.localeCompare(b.label, undefined, { sensitivity: 'base' })
  })
}

// Project drill-in lanes are git-driven: source them from `git worktree list`
// so linked worktrees still appear even when their sessions aren't in the
// recents payload currently loaded in memory.
function useRepoWorktreeMap(repoPaths: string[], enabled: boolean): [Record<string, HermesGitWorktree[]>, boolean] {
  const [map, setMap] = useState<Record<string, HermesGitWorktree[]>>({})
  const [loading, setLoading] = useState(false)
  const key = useMemo(() => pathListKey(repoPaths), [repoPaths])
  // Refetch when a worktree is added/removed so a new lane shows immediately.
  const refreshToken = useStore($worktreeRefreshToken)

  useEffect(() => {
    const git = window.hermesDesktop?.git

    if (!enabled || !repoPaths.length || !git?.worktreeList) {
      setMap({})
      setLoading(false)

      return
    }

    let cancelled = false

    setLoading(true)
    void Promise.all(
      repoPaths.map(async repoPath => {
        try {
          const worktrees = await git.worktreeList(repoPath)

          return [repoPath, worktrees] as const
        } catch {
          return [repoPath, []] as const
        }
      })
    )
      .then(entries => {
        if (!cancelled) {
          setMap(Object.fromEntries(entries))
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [enabled, key, repoPaths, refreshToken])

  return [map, loading]
}

function orderByIds<T>(items: T[], getId: (item: T) => string, orderIds: string[]): T[] {
  if (!orderIds.length) {
    return items
  }

  const byId = new Map(items.map(item => [getId(item), item]))
  const seen = new Set<string>()
  const ordered: T[] = []

  for (const id of orderIds) {
    const item = byId.get(id)

    if (item) {
      ordered.push(item)
      seen.add(id)
    }
  }

  // Items missing from the persisted order are new since it was last
  // reconciled. Callers pass recency-sorted lists (newest first), so surface
  // these at the TOP instead of burying them beneath the saved order —
  // otherwise a brand-new session sinks to the bottom of the sidebar and reads
  // as "my latest session never showed up".
  const fresh = items.filter(item => !seen.has(getId(item)))

  return fresh.length ? [...fresh, ...ordered] : ordered
}

function reconcileOrderIds(currentIds: string[], orderIds: string[]): string[] {
  if (!currentIds.length) {
    return []
  }

  if (!orderIds.length) {
    return currentIds
  }

  return reconcileFreshFirst(currentIds, orderIds)
}

function sameIds(left: string[], right: string[]) {
  return left.length === right.length && left.every((item, index) => item === right[index])
}

// FTS results cover sessions that aren't in the loaded page; synthesize a
// minimal SessionInfo so they render in the same row component (resume works
// by id; the snippet stands in for the preview).
function searchResultToSession(result: SessionSearchResult): SessionInfo {
  const ts = result.session_started ?? Date.now() / 1000

  return {
    archived: false,
    cwd: null,
    ended_at: null,
    id: result.session_id,
    _lineage_root_id: result.lineage_root ?? null,
    input_tokens: 0,
    is_active: false,
    last_active: ts,
    message_count: 0,
    model: result.model ?? null,
    output_tokens: 0,
    preview: result.snippet?.trim() || null,
    source: result.source ?? null,
    started_at: ts,
    title: null,
    tool_call_count: 0
  }
}

// Persisted open/collapse for a repo/worktree node (absent = open). Lets a
// project's folder layout auto-restore when you enter it, and survive reloads.
function useWorkspaceNodeOpen(id: string): [boolean, () => void] {
  const collapsed = useStore($sidebarWorkspaceCollapsedIds)
  const open = !collapsed.includes(id)

  return [open, () => toggleWorkspaceNodeCollapsed(id)]
}

function useSortableBindings(id: string) {
  const { attributes, isDragging, listeners, setNodeRef, transform, transition } = useSortable({ id })

  return {
    dragging: isDragging,
    dragHandleProps: { ...attributes, ...listeners },
    ref: setNodeRef,
    reorderable: true as const,
    style: {
      // Uniform vertical list: only ever translate on Y. Ignoring x and the
      // scaleX/scaleY that CSS.Transform.toString would emit keeps a dragged
      // group/row from drifting sideways or morphing its size mid-drag.
      transform: transform ? `translate3d(0px, ${transform.y}px, 0)` : undefined,
      transition: isDragging ? undefined : transition,
      willChange: isDragging ? 'transform' : undefined
    }
  }
}

interface ChatSidebarProps extends React.ComponentProps<typeof Sidebar> {
  currentView: AppView
  onNavigate: (item: SidebarNavItem) => void
  onLoadMoreSessions: () => Promise<void> | void
  onLoadMoreProfileSessions?: (profile: string) => Promise<void> | void
  onLoadMoreMessaging?: (platform: string) => Promise<void> | void
  onResumeSession: (sessionId: string) => void
  onDeleteSession: (sessionId: string) => void
  onArchiveSession: (sessionId: string) => void
  onNewSessionInWorkspace: (path: null | string) => void
  onManageCronJob: (jobId: string) => void
  onTriggerCronJob: (jobId: string) => void
}

export function ChatSidebar({
  currentView,
  onNavigate,
  onLoadMoreSessions,
  onLoadMoreProfileSessions,
  onLoadMoreMessaging,
  onResumeSession,
  onDeleteSession,
  onArchiveSession,
  onNewSessionInWorkspace,
  onManageCronJob,
  onTriggerCronJob
}: ChatSidebarProps) {
  const { t } = useI18n()
  const s = t.sidebar
  const sidebarOpen = useStore($sidebarOpen)
  // Collapsed-but-overlay-mounted → render the full sidebar, not just the nav rail.
  const overlayMounted = useStore($sidebarOverlayMounted)
  const contentVisible = sidebarOpen || overlayMounted
  const panesFlipped = useStore($panesFlipped)
  const agentsGrouped = useStore($sidebarAgentsGrouped)
  const pinnedSessionIds = useStore($pinnedSessionIds)
  const pinsOpen = useStore($sidebarPinsOpen)
  const agentsOpen = useStore($sidebarRecentsOpen)
  const cronOpen = useStore($sidebarCronOpen)
  const selectedSessionId = useStore($selectedStoredSessionId)
  const sessions = useStore($sessions)
  const cronSessions = useStore($cronSessions)
  const cronJobs = useStore($cronJobs)
  const messagingSessions = useStore($messagingSessions)
  const messagingPlatformTotals = useStore($messagingPlatformTotals)
  const messagingTruncated = useStore($messagingTruncated)
  const sessionsLoading = useStore($sessionsLoading)
  const sessionsTotal = useStore($sessionsTotal)
  const sessionProfileTotals = useStore($sessionProfileTotals)
  const workingSessionIds = useStore($workingSessionIds)
  const profiles = useStore($profiles)
  const profileScope = useStore($profileScope)
  // Only surface the profile switcher when more than one profile exists, so
  // single-profile users see the unchanged sidebar.
  const multiProfile = profiles.length > 1
  // Gate ALL-profiles grouping on multiProfile too: if a user drops back to one
  // profile while scope is still ALL (persisted), the rail is hidden and they'd
  // otherwise be stuck in the grouped view with no way out.
  const showAllProfiles = multiProfile && profileScope === ALL_PROFILES
  const agentOrderIds = useStore($sidebarSessionOrderIds)
  const agentOrderManual = useStore($sidebarSessionOrderManual)
  const workspaceOrderIds = useStore($sidebarWorkspaceOrderIds)
  const workspaceParentOrderIds = useStore($sidebarWorkspaceParentOrderIds)
  const projects = useStore($projects)
  const projectTree = useStore($projectTree)
  const projectTreeLoading = useStore($projectTreeLoading)
  const scopedSessionIds = useStore($scopedSessionIds)
  const reposScanning = useStore($reposScanning)
  const activeProjectId = useStore($activeProjectId)
  const projectScope = useStore($projectScope)
  const currentCwd = useStore($currentCwd)
  const gatewayState = useStore($gatewayState)
  const dismissedAutoProjects = useStore($dismissedAutoProjectIds)
  const [searchQuery, setSearchQuery] = useState('')
  const [serverMatches, setServerMatches] = useState<SessionSearchResult[]>([])
  const [searchPending, setSearchPending] = useState(false)
  const [newSessionKbdFlash, setNewSessionKbdFlash] = useState(false)
  const [profileLoadMorePending, setProfileLoadMorePending] = useState<Record<string, boolean>>({})
  const [messagingLoadMorePending, setMessagingLoadMorePending] = useState<Record<string, boolean>>({})
  const [recentsLoadMorePending, setRecentsLoadMorePending] = useState(false)
  const messagingOpenIds = useStore($sidebarMessagingOpenIds)
  // Per-platform count of rows currently revealed (starts at NON_SESSION_INITIAL_ROWS).
  const [messagingVisible, setMessagingVisible] = useState<Record<string, number>>({})
  const searchInputRef = useRef<HTMLInputElement>(null)
  const trimmedQuery = searchQuery.trim()

  // Hotkey (session.focusSearch) → focus the field once it's mounted.
  useEffect(() => {
    const onFocus = () => searchInputRef.current?.focus({ preventScroll: true })

    window.addEventListener(SESSION_SEARCH_FOCUS_EVENT, onFocus)

    return () => window.removeEventListener(SESSION_SEARCH_FOCUS_EVENT, onFocus)
  }, [])

  // Flash the ⌘N hint full-opacity (no transition) for the press, so hitting
  // the shortcut visibly pings its affordance in the sidebar.
  useEffect(() => {
    let timeout: ReturnType<typeof setTimeout> | undefined

    const onShortcut = () => {
      setNewSessionKbdFlash(true)
      clearTimeout(timeout)
      timeout = setTimeout(() => setNewSessionKbdFlash(false), 140)
    }

    window.addEventListener('hermes:new-session-shortcut', onShortcut)

    return () => {
      window.removeEventListener('hermes:new-session-shortcut', onShortcut)
      clearTimeout(timeout)
    }
  }, [])

  const activeSidebarSessionId = currentView === 'chat' ? selectedSessionId : null

  const dndSensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )

  // Profile scope = the "workspace switcher" context. Concrete scope shows only
  // that profile's sessions (clean rows, no per-row tags); ALL fans every
  // profile in, grouped by profile below. Single-profile users land here with
  // scope === their only profile, so nothing is filtered out.
  const visibleSessions = useMemo(
    () => (showAllProfiles ? sessions : sessions.filter(s => normalizeProfileKey(s.profile) === profileScope)),
    [sessions, showAllProfiles, profileScope]
  )

  // Agent session order is pinned to creation time (started_at), NOT activity —
  // a new message must never float a session to the top. Position only changes
  // for a brand-new session or an explicit manual drag (agentOrderIds).
  const sortedSessions = useMemo(
    () => [...visibleSessions].sort((a, b) => (b.started_at || 0) - (a.started_at || 0)),
    [visibleSessions]
  )

  const workingSessionIdSet = useMemo(() => new Set(workingSessionIds), [workingSessionIds])

  // Index sessions by both their live id and their lineage-root id so a pin
  // stored as the pre-compression root resolves to the live continuation tip.
  const sessionByAnyId = useMemo(() => {
    const map = new Map<string, SessionInfo>()

    // Cron sessions are listed separately but can still be pinned, so index
    // them too — otherwise a pinned cron job can't resolve into the Pinned
    // section. Recents take precedence on id collisions (set last).
    for (const s of [...cronSessions, ...visibleSessions]) {
      map.set(s.id, s)

      if (s._lineage_root_id && !map.has(s._lineage_root_id)) {
        map.set(s._lineage_root_id, s)
      }
    }

    return map
  }, [visibleSessions, cronSessions])

  const pinnedSessions = useMemo(() => {
    const seen = new Set<string>()
    const out: SessionInfo[] = []

    for (const pinId of pinnedSessionIds) {
      const session = sessionByAnyId.get(pinId)

      if (session && !seen.has(session.id)) {
        seen.add(session.id)
        out.push(session)
      }
    }

    return out
  }, [pinnedSessionIds, sessionByAnyId])

  const pinnedRealIdSet = useMemo(() => new Set(pinnedSessions.map(s => s.id)), [pinnedSessions])

  // Full-text search across *all* sessions (not just the loaded page) so 699
  // sessions stay findable. Debounced; loaded sessions are matched instantly
  // client-side and merged ahead of the server hits.
  useEffect(() => {
    if (!trimmedQuery) {
      setServerMatches([])
      setSearchPending(false)

      return
    }

    let cancelled = false

    setSearchPending(true)

    const id = window.setTimeout(() => {
      void searchSessions(trimmedQuery)
        .then(res => {
          if (!cancelled) {
            setServerMatches(res.results)
          }
        })
        .catch(() => undefined)
        .finally(() => {
          if (!cancelled) {
            setSearchPending(false)
          }
        })
    }, 200)

    return () => {
      cancelled = true
      window.clearTimeout(id)
    }
  }, [trimmedQuery])

  const searchResults = useMemo(() => {
    if (!trimmedQuery) {
      return []
    }

    const out = new Map<string, SessionInfo>()

    for (const s of sortedSessions) {
      if (sessionMatchesSearch(s, trimmedQuery)) {
        out.set(s.id, s)
      }
    }

    for (const match of serverMatches) {
      if (out.has(match.session_id)) {
        continue
      }

      const loaded = sessionByAnyId.get(match.session_id)
      out.set(match.session_id, loaded ?? searchResultToSession(match))
    }

    return [...out.values()]
  }, [trimmedQuery, sortedSessions, serverMatches, sessionByAnyId])

  const unpinnedAgentSessions = useMemo(
    () => sortedSessions.filter(s => !pinnedRealIdSet.has(s.id)),
    [sortedSessions, pinnedRealIdSet]
  )

  useEffect(() => {
    const next = resolveManualSessionOrderIds(
      unpinnedAgentSessions.map(s => s.id),
      agentOrderIds,
      agentOrderManual
    )

    if (!next.length && agentOrderManual) {
      setSidebarSessionOrderManual(false)
    }

    if (!next.length && agentOrderIds.length) {
      setSidebarSessionOrderIds([])

      return
    }

    if (next.length && !sameIds(next, agentOrderIds)) {
      setSidebarSessionOrderIds(next)
    }
  }, [agentOrderIds, agentOrderManual, unpinnedAgentSessions])

  const agentSessions = useMemo(
    () => (agentOrderManual ? orderByIds(unpinnedAgentSessions, s => s.id, agentOrderIds) : unpinnedAgentSessions),
    [unpinnedAgentSessions, agentOrderIds, agentOrderManual]
  )

  // Recents are local-only: messaging-platform sessions are fetched as their
  // own slice ($messagingSessions) and rendered in self-managed per-platform
  // sections below, so there is no source-grouping magic to untangle here.
  //
  // Workspace grouping is a `project -> repo -> lane -> sessions` tree computed
  // authoritatively on the backend (projects.tree). Parents reorder via
  // workspaceParentOrderIds; worktrees within a parent via workspaceOrderIds.
  const worktreeGroupingActive = agentsGrouped && !showAllProfiles
  const gatewayReady = gatewayState === 'open'

  // The project tree (membership, repos, lanes, counts) is computed
  // authoritatively on the backend. Refresh it whenever the grouped view is
  // active and the gateway is up; the disk scan (once per run) folds any
  // zero-session repos into the same tree. Best-effort: failures keep the
  // cached tree so the sidebar doesn't flicker.
  useEffect(() => {
    if (worktreeGroupingActive && gatewayReady) {
      void refreshProjects()
      void refreshProjectTree()
      void scanAndRecordRepos()
    }
  }, [worktreeGroupingActive, profileScope, gatewayReady])

  // Re-fetch the tree whenever the recents set changes. `$sessions` is replaced
  // on the same edges that matter here (a turn completing, a session created /
  // resumed / deleted / archived), so a session started from the overview shows
  // up under its project once it has activity — without a manual view toggle.
  useEffect(() => {
    if (worktreeGroupingActive && gatewayReady) {
      void refreshProjectTree()
    }
  }, [worktreeGroupingActive, gatewayReady, sessions])

  // Apply the persisted repo + worktree orders to a project's repo subtrees.
  const orderRepos = useCallback(
    (repos: SidebarWorkspaceTree[]): SidebarWorkspaceTree[] =>
      orderByIds(repos, parent => parent.id, workspaceParentOrderIds).map(parent => ({
        ...parent,
        groups: orderByIds(parent.groups, group => group.id, workspaceOrderIds)
      })),
    [workspaceParentOrderIds, workspaceOrderIds]
  )

  // ── Projects: the single top-level model (authoritative, from the backend) ──
  // `projects.tree` already unifies explicit projects + auto repos and folds
  // linked worktrees under their main repo. The desktop only layers local view
  // state on top: dismissed auto-projects, persisted repo/lane order, and the
  // overview sort. Membership (scopedSessionIds) comes straight from the store.
  const projectModel = useMemo<SidebarProjectTree[]>(() => {
    if (showAllProfiles) {
      return []
    }

    const dismissed = new Set(dismissedAutoProjects)

    return sortProjectsForOverview(
      projectTree
        .filter(node => !(node.isAuto && dismissed.has(node.id)))
        .map(project => ({ ...project, repos: orderRepos(project.repos) })),
      activeProjectId
    )
  }, [showAllProfiles, projectTree, dismissedAutoProjects, orderRepos, activeProjectId])

  // The overview only renders in grouped mode; the model stays live regardless
  // so scoping is consistent across views.
  const agentProjectTree = worktreeGroupingActive ? projectModel : undefined

  // ── Project switcher (drill-in) ────────────────────────────────────────────
  // Grouped, single-profile view is a project switcher: ALL_PROJECTS shows the
  // overview (a list you click into); a concrete scope means you've "entered" a
  // project, so the Sessions list shows ONLY that project's worktrees/sessions.
  const projectsActive = Boolean(agentProjectTree?.length)

  // The overview node for the entered project (structure + counts, empty lanes).
  const overviewEnteredProject =
    projectsActive && projectScope !== ALL_PROJECTS
      ? agentProjectTree?.find(node => node.id === projectScope)
      : undefined

  const inProject = Boolean(overviewEnteredProject)
  const enteredProjectId = overviewEnteredProject?.id

  // Entering a project lazily hydrates its full lanes (repo -> lane -> sessions)
  // from the backend — same grouping/ids as the overview, just with rows.
  const [enteredProjectTree, setEnteredProjectTree] = useState<SidebarProjectTree | null>(null)
  const [enteredProjectLoading, setEnteredProjectLoading] = useState(false)

  useEffect(() => {
    if (!enteredProjectId || !gatewayReady) {
      setEnteredProjectTree(null)
      setEnteredProjectLoading(false)

      return
    }

    let cancelled = false

    setEnteredProjectLoading(true)
    void fetchProjectSessions(enteredProjectId)
      .then(project => {
        if (!cancelled) {
          setEnteredProjectTree(project)
        }
      })
      .finally(() => {
        if (!cancelled) {
          setEnteredProjectLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
    // `projectTree` in deps: re-hydrate after a tree refresh so the entered view
    // stays current with new/ended sessions.
  }, [enteredProjectId, gatewayReady, projectTree])

  // Prefer the hydrated tree; fall back to the overview node (empty lanes) while
  // the drill-in fetch is in flight, so the header/structure render immediately.
  const enteredProject = useMemo<SidebarProjectTree | undefined>(() => {
    if (!overviewEnteredProject) {
      return undefined
    }

    const hydrated =
      enteredProjectTree && enteredProjectTree.id === overviewEnteredProject.id
        ? enteredProjectTree
        : overviewEnteredProject

    // Overlay live $sessions so a session started here appears instantly with
    // its working arc, exactly like the flat Recents list (the backend snapshot
    // catches up on the next tree refresh).
    const live = overlayLiveLanes(hydrated, agentSessions, projects)

    return { ...live, repos: orderRepos(live.repos) }
  }, [overviewEnteredProject, enteredProjectTree, agentSessions, projects, orderRepos])

  const scopedRepoPaths = useMemo(
    () =>
      enteredProject ? enteredProject.repos.map(repo => repo.path).filter((path): path is string => Boolean(path)) : [],
    [enteredProject]
  )

  // git worktree list is a VISUAL-only enhancer (empty lanes); never membership.
  const [scopedRepoWorktrees, scopedRepoWorktreesLoading] = useRepoWorktreeMap(
    scopedRepoPaths,
    Boolean(enteredProject && !showAllProfiles)
  )

  const scopedLanesLoading = enteredProjectLoading || scopedRepoWorktreesLoading

  const lastProjectCwdSyncRef = useRef<null | string>(null)

  const syncProjectCwd = useCallback(
    (project: SidebarProjectTree) => {
      const target = projectTreeCwd(project)

      if (target && target !== currentCwd) {
        setCurrentCwd(target)
      }
    },
    [currentCwd]
  )

  useEffect(() => {
    if (!inProject || !enteredProject) {
      lastProjectCwdSyncRef.current = null

      return
    }

    if (lastProjectCwdSyncRef.current === enteredProject.id) {
      return
    }

    syncProjectCwd(enteredProject)
    lastProjectCwdSyncRef.current = enteredProject.id
  }, [inProject, enteredProject, syncProjectCwd])

  // A persisted scope can go stale (project archived/removed, or a profile
  // switch swapped the whole catalog). Once projects have loaded, drop back to
  // the overview if the scoped id is gone.
  useEffect(() => {
    if (projectScope !== ALL_PROJECTS && projectsActive && !enteredProject) {
      exitProjectScope()
    }
  }, [projectScope, projectsActive, enteredProject])

  // The project overview (drill-in list) vs. the entered project's content.
  const projectOverview = projectsActive && !inProject ? agentProjectTree : undefined

  // Preview rows come from the backend tree (each project carries its
  // most-recent sessions), overlaid with live $sessions so a just-created
  // session shows under its project instantly (and with its working arc),
  // matching the flat Recents list. Keyed by project path for the rows.
  const overviewPreviews = useMemo<Record<string, SessionInfo[]>>(
    () => overlayLivePreviews(projectOverview ?? [], agentSessions, projects, PROJECT_PREVIEW_COUNT),
    [projectOverview, agentSessions, projects]
  )

  const onEnterProject = useCallback(
    (id: string) => {
      const project = projectModel.find(node => node.id === id)

      if (project) {
        syncProjectCwd(project)
      }

      enterProject(id)
    },
    [projectModel, syncProjectCwd]
  )

  // The Sessions section is a project switcher in grouped mode: its label reads
  // "Sessions" when flat, "Projects" at the overview, and the project's name
  // once you've entered one.
  const sessionsLabel =
    inProject && enteredProject ? enteredProject.label : worktreeGroupingActive ? s.projects.sectionLabel : s.sessions

  const runKeyedLoad = useCallback(
    (
      key: string,
      load: ((key: string) => Promise<void> | void) | undefined,
      setPending: React.Dispatch<React.SetStateAction<Record<string, boolean>>>
    ) => {
      if (!load) {
        return
      }

      setPending(prev => ({ ...prev, [key]: true }))

      void Promise.resolve(load(key))
        .catch(() => undefined)
        .finally(() => setPending(({ [key]: _done, ...rest }) => rest))
    },
    []
  )

  const loadMoreForProfileGroup = useCallback(
    (profile: string) => runKeyedLoad(profile, onLoadMoreProfileSessions, setProfileLoadMorePending),
    [onLoadMoreProfileSessions, runKeyedLoad]
  )

  const loadMoreForMessaging = useCallback(
    (platform: string) => runKeyedLoad(platform, onLoadMoreMessaging, setMessagingLoadMorePending),
    [onLoadMoreMessaging, runKeyedLoad]
  )

  // Reveal another batch of a platform's rows; fetch from the backend too if we
  // run past what's loaded and more remain on disk.
  const revealMoreMessaging = (platform: string, loaded: number, hasMore: boolean) => {
    const next = (messagingVisible[platform] ?? NON_SESSION_INITIAL_ROWS) + NON_SESSION_LOAD_STEP

    setMessagingVisible(prev => ({ ...prev, [platform]: next }))

    if (next > loaded && hasMore) {
      loadMoreForMessaging(platform)
    }
  }

  // Each messaging platform is its own self-managed section: split the
  // separately-fetched messaging slice by source, newest platform first, rows
  // within a platform by recency. Per-platform totals (when a "load more" has
  // resolved them) drive the count + whether more remain on disk.
  const messagingGroups = useMemo<MessagingSection[]>(() => {
    if (!messagingSessions.length) {
      return []
    }

    const bySource = new Map<string, SessionInfo[]>()

    for (const session of messagingSessions) {
      const sourceId = normalizeSessionSource(session.source)

      if (!sourceId) {
        continue
      }

      const list = bySource.get(sourceId) ?? []
      list.push(session)
      bySource.set(sourceId, list)
    }

    return [...bySource.entries()]
      .map(([sourceId, list]) => {
        const ordered = [...list].sort((a, b) => sessionTime(b) - sessionTime(a))
        const known = messagingPlatformTotals[sourceId]
        const total = Math.max(ordered.length, known ?? 0)

        return {
          // Known exact total → more exist iff total exceeds loaded; otherwise
          // the seed fetch was capped, so assume more until a per-platform load
          // resolves the count.
          hasMore: known != null ? known > ordered.length : messagingTruncated,
          label: sessionSourceLabel(sourceId) ?? sourceId,
          sessions: ordered,
          sourceId,
          total
        }
      })
      .sort((a, b) => sessionTime(b.sessions[0]) - sessionTime(a.sessions[0]))
  }, [messagingSessions, messagingPlatformTotals, messagingTruncated])

  // ALL-profiles view: one collapsible group per profile, color on the header
  // (not on every row). Default profile floats to the top, the rest alpha.
  const profileGroups = useMemo<SidebarSessionGroup[] | undefined>(() => {
    if (!showAllProfiles) {
      return undefined
    }

    const groups = new Map<string, SidebarSessionGroup>()

    for (const session of agentSessions) {
      const key = normalizeProfileKey(session.profile)

      const group = groups.get(key) ?? {
        color: profileColor(key),
        id: key,
        label: key,
        mode: 'profile',
        path: null,
        sessions: []
      }

      group.sessions.push(session)

      groups.set(key, group)
    }

    return (
      [...groups.values()]
        .map(group => ({
          ...group,
          loadingMore: Boolean(profileLoadMorePending[group.id]),
          onLoadMore: onLoadMoreProfileSessions ? () => loadMoreForProfileGroup(group.id) : undefined,
          totalCount: Math.max(group.sessions.length, sessionProfileTotals[group.id] ?? 0)
        }))
        // default (root) first, then the rest alphabetically.
        .sort((a, b) => (a.id === 'default' ? -1 : b.id === 'default' ? 1 : a.label.localeCompare(b.label)))
    )
  }, [
    showAllProfiles,
    agentSessions,
    loadMoreForProfileGroup,
    onLoadMoreProfileSessions,
    profileLoadMorePending,
    sessionProfileTotals
  ])

  // The flat recents list is everything NOT claimed by a project — loose,
  // cwd-less, or unplaceable sessions (see scopedSessionIds, the single source).
  const displayAgentSessions = useMemo(
    () => (scopedSessionIds.size ? agentSessions.filter(session => !scopedSessionIds.has(session.id)) : agentSessions),
    [agentSessions, scopedSessionIds]
  )

  // Pagination is scope-aware. In "All profiles" mode it tracks the global
  // unified set. When scoped to one profile it must compare that profile's own
  // loaded rows against that profile's total — otherwise a huge default profile
  // keeps "Load more" stuck on while you browse a small one (the aggregator's
  // total sums every profile). Per-profile totals come from the aggregator
  // (children excluded); fall back to the global total / loaded count.
  const loadedSessionCount = showAllProfiles ? sessions.length : visibleSessions.length
  const scopedProfileTotal = showAllProfiles ? undefined : sessionProfileTotals[profileScope]

  const knownSessionTotal = Math.max(
    showAllProfiles ? sessionsTotal : (scopedProfileTotal ?? loadedSessionCount),
    loadedSessionCount
  )

  const hasMoreSessions = knownSessionTotal > loadedSessionCount

  const recentsMeta = countLabel(displayAgentSessions.length, knownSessionTotal)
  const displayRecentsCountRef = useRef(0)
  const loadedRecentsCountRef = useRef(0)
  displayRecentsCountRef.current = displayAgentSessions.length
  loadedRecentsCountRef.current = loadedSessionCount

  const onLoadMoreRecents = useCallback(async () => {
    if (recentsLoadMorePending) {
      return
    }

    setRecentsLoadMorePending(true)

    try {
      const startVisible = displayRecentsCountRef.current
      const targetVisible = startVisible + SIDEBAR_SESSIONS_PAGE_SIZE
      let lastLoaded = loadedRecentsCountRef.current

      // Project-less recents can be sparse in the global recent stream (because
      // project-scoped sessions are filtered out in the UI). Keep paging until
      // we actually reveal a full page of visible rows, or the backend window
      // stops growing.
      for (let attempt = 0; attempt < 6; attempt += 1) {
        await Promise.resolve(onLoadMoreSessions())
        await new Promise<void>(resolve => window.requestAnimationFrame(() => resolve()))

        const visibleNow = displayRecentsCountRef.current
        const loadedNow = loadedRecentsCountRef.current

        if (visibleNow >= targetVisible) {
          break
        }

        if (loadedNow <= lastLoaded) {
          break
        }

        lastLoaded = loadedNow
      }
    } finally {
      setRecentsLoadMorePending(false)
    }
  }, [onLoadMoreSessions, recentsLoadMorePending])

  const displayAgentGroups = showAllProfiles ? profileGroups : undefined

  // The recents list owns its own (virtualized) scroll container only when it's a
  // long flat list. In that case it must keep its scroller even in short mode, so
  // we don't flatten it (flattening would defeat virtualization). Short flat lists
  // and grouped views (profile groups or the worktree tree) flatten into the
  // single outer scroll instead.
  // Whichever grouping is active, the flat set of repo subtrees on screen — the
  // single source for reconciling repo/worktree order, whether repos hang off
  // the bare tree or are nested under projects.
  const activeRepoTrees = useMemo<SidebarWorkspaceTree[]>(
    () => (agentProjectTree ? agentProjectTree.flatMap(project => project.repos) : []),
    [agentProjectTree]
  )

  const recentsVirtualizes =
    !displayAgentGroups?.length &&
    !agentProjectTree?.length &&
    displayAgentSessions.length >= VIRTUALIZE_THRESHOLD

  // Keep the persisted parent + worktree orders reconciled with what's on screen:
  // freshly-seen repos/worktrees surface at the top, vanished ones drop out of
  // the saved order.
  useEffect(() => {
    if (!activeRepoTrees.length) {
      return
    }

    const nextParents = reconcileOrderIds(
      activeRepoTrees.map(parent => parent.id),
      workspaceParentOrderIds
    )

    if (!sameIds(nextParents, workspaceParentOrderIds)) {
      setSidebarWorkspaceParentOrderIds(nextParents)
    }

    const nextWorktrees = reconcileOrderIds(
      activeRepoTrees.flatMap(parent => parent.groups.map(group => group.id)),
      workspaceOrderIds
    )

    if (!sameIds(nextWorktrees, workspaceOrderIds)) {
      setSidebarWorkspaceOrderIds(nextWorktrees)
    }
  }, [activeRepoTrees, workspaceParentOrderIds, workspaceOrderIds])

  const showSessionSkeletons = sessionsLoading && sortedSessions.length === 0

  const showSessionSections = showSessionSkeletons || sortedSessions.length > 0

  // Each reorderable list reports its OWN new id order; persisting is a direct,
  // typed write — no id-prefix sniffing to figure out which level moved.
  const reorderSessions = (ids: string[]) => {
    setSidebarSessionOrderManual(true)
    setSidebarSessionOrderIds(ids)
  }

  // Sortable rows carry live session ids; the pinned store is keyed by durable
  // (lineage-root) ids, so translate before persisting the new order.
  const reorderPinned = (ids: string[]) =>
    setPinnedSessionOrder(
      ids.map(id => {
        const session = sessionByAnyId.get(id)

        return session ? sessionPinId(session) : id
      })
    )

  return (
    <Sidebar
      className={cn(
        'relative h-full min-w-0 overflow-hidden border-t-0 border-b-0 text-foreground transition-none',
        panesFlipped ? 'border-l border-r-0' : 'border-r border-l-0',
        sidebarOpen
          ? 'border-(--sidebar-edge-border) bg-(--ui-sidebar-surface-background) opacity-100'
          : 'pointer-events-none border-transparent bg-transparent opacity-0',
        // While floated by PaneShell's hover-reveal, force visible + interactive
        // — on hover (group-hover/reveal) or when keyboard-pinned (data-forced).
        'in-data-[pane-hover-reveal=open]:pointer-events-auto in-data-[pane-hover-reveal=open]:border-(--sidebar-edge-border) in-data-[pane-hover-reveal=open]:bg-(--ui-sidebar-surface-background) in-data-[pane-hover-reveal=open]:opacity-100',
        'group-hover/reveal:pointer-events-auto group-hover/reveal:border-(--sidebar-edge-border) group-hover/reveal:bg-(--ui-sidebar-surface-background) group-hover/reveal:opacity-100'
      )}
      collapsible="none"
    >
      <SidebarContent className="gap-0 overflow-hidden bg-transparent px-2.5">
        <SidebarGroup className="shrink-0 p-0 pb-2 pt-[calc(var(--titlebar-height)+0.375rem)]">
          <SidebarGroupContent>
            <SidebarMenu className="gap-px">
              {SIDEBAR_NAV.map(item => {
                const isInteractive = Boolean(item.action) || Boolean(item.route)

                const active =
                  (item.id === 'skills' && currentView === 'skills') ||
                  (item.id === 'messaging' && currentView === 'messaging') ||
                  (item.id === 'artifacts' && currentView === 'artifacts')

                const isNewSession = item.id === 'new-session'

                return (
                  <SidebarMenuItem key={item.id}>
                    <SidebarMenuButton
                      aria-disabled={!isInteractive}
                      className={cn(
                        // no-drag: these rows sit directly under the titlebar's
                        // [-webkit-app-region:drag] strips (app-shell.tsx), with only
                        // 6px of clearance. Drag regions win hit-testing over DOM
                        // (pointer-events can't override), and on Linux/WSLg the
                        // resolved region has been observed to swallow clicks on the
                        // top rows. Same carve-out as USER_BUBBLE_BASE_CLASS in
                        // thread.tsx.
                        'flex h-7 w-full justify-start gap-2 rounded-md border border-transparent px-2 text-left text-[0.8125rem] font-medium text-(--ui-text-secondary) transition-colors duration-100 ease-out [-webkit-app-region:no-drag] hover:bg-(--ui-control-hover-background) hover:text-foreground hover:transition-none',
                        active &&
                          'border-(--ui-stroke-tertiary) bg-(--ui-control-active-background) text-foreground shadow-none hover:border-(--ui-stroke-tertiary)!',
                        !isInteractive &&
                          'cursor-default hover:border-transparent hover:bg-transparent hover:text-inherit'
                      )}
                      onClick={() => {
                        // A plain new session lands in whatever profile the live
                        // gateway is on (= the active switcher context). null →
                        // no swap. The switcher header is the single place to
                        // change which profile that is.
                        if (isNewSession) {
                          $newChatProfile.set(null)
                        }

                        onNavigate(item)
                      }}
                      tooltip={s.nav[item.id] ?? item.label}
                      type="button"
                    >
                      <item.icon className="size-4 shrink-0 text-[color-mix(in_srgb,currentColor_72%,transparent)]" />
                      {contentVisible && (
                        <>
                          <span className="min-w-0 flex-1 truncate">{s.nav[item.id] ?? item.label}</span>
                          {isNewSession && (
                            <KbdGroup
                              className={cn('ml-auto opacity-55', newSessionKbdFlash && 'opacity-100!')}
                              keys={[...NEW_SESSION_KBD]}
                              size="sm"
                            />
                          )}
                        </>
                      )}
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                )
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {contentVisible && showSessionSections && (
          <div className="shrink-0 px-2 pb-1 pt-1">
            <SearchField
              aria-label={s.searchAria}
              inputRef={searchInputRef}
              onChange={setSearchQuery}
              placeholder={s.searchPlaceholder}
              value={searchQuery}
            />
          </div>
        )}

        {contentVisible && showSessionSections && (
          <div className={cn('flex min-h-0 flex-1 flex-col pb-1.75', SCROLL_Y)}>
            {trimmedQuery && (
              <SidebarSessionsSection
                activeSessionId={activeSidebarSessionId}
                contentClassName={cn('flex min-h-0 flex-1 flex-col gap-px pb-1.75', SCROLL_Y)}
                emptyState={
                  searchPending ? (
                    <SidebarSessionSkeletons />
                  ) : (
                    <div className="grid min-h-24 place-items-center rounded-lg px-2 text-center text-xs text-(--ui-text-tertiary)">
                      {s.noMatch(trimmedQuery)}
                    </div>
                  )
                }
                label={s.results}
                labelMeta={String(searchResults.length)}
                onArchiveSession={onArchiveSession}
                onDeleteSession={onDeleteSession}
                onResumeSession={onResumeSession}
                onToggle={() => undefined}
                onTogglePin={pinSession}
                open
                pinned={false}
                rootClassName="min-h-32 flex-1 overflow-hidden p-0"
                sessions={searchResults}
                workingSessionIdSet={workingSessionIdSet}
              />
            )}

            {!trimmedQuery && (
              <SidebarSessionsSection
                activeSessionId={activeSidebarSessionId}
                contentClassName={cn('flex max-h-44 flex-col gap-px rounded-lg pb-2 pt-1', GROUP_BODY)}
                dndSensors={dndSensors}
                emptyState={<SidebarPinnedEmptyState />}
                label={s.pinned}
                onArchiveSession={onArchiveSession}
                onDeleteSession={onDeleteSession}
                onReorderSessions={reorderPinned}
                onResumeSession={onResumeSession}
                onToggle={() => setSidebarPinsOpen(!pinsOpen)}
                onTogglePin={unpinSession}
                open={pinsOpen}
                pinned
                rootClassName="shrink-0 p-0 pb-1"
                sessions={pinnedSessions}
                sortable={pinnedSessions.length > 1}
                workingSessionIdSet={workingSessionIdSet}
              />
            )}

            {!trimmedQuery && (
              <SidebarSessionsSection
                activeProjectId={activeProjectId}
                activeSessionId={activeSidebarSessionId}
                collapsible={!inProject}
                contentClassName={cn(
                  'flex min-h-0 flex-1 flex-col pb-1.75',
                  SCROLL_Y,
                  // Separate profile sections clearly in the ALL view; rows inside
                  // each group keep their own tight gap-px rhythm.
                  showAllProfiles ? 'gap-3' : 'gap-px',
                  // Flatten into the single scroll when compact — unless this is the
                  // virtualized long list, which must keep its own scroller.
                  !recentsVirtualizes && COMPACT_FLAT
                )}
                dndSensors={dndSensors}
                emptyState={
                  showSessionSkeletons ? (
                    <SidebarSessionSkeletons />
                  ) : (
                    <div className="grid min-h-16 place-items-center rounded-lg px-2 text-center text-xs text-(--ui-text-tertiary)">
                      {inProject ? s.projectEmpty : pinnedSessions.length > 0 ? s.allPinned : s.noSessions}
                    </div>
                  )
                }
                footer={
                  // Hide "load more" only when workspace-grouped (those groups page
                  // themselves). ALL-profiles now pages per-profile from each profile
                  // header; the global footer only applies to non-ALL views.
                  !showAllProfiles && !agentsGrouped && !showSessionSkeletons && hasMoreSessions ? (
                    <SidebarLoadMoreRow
                      loading={sessionsLoading || recentsLoadMorePending}
                      onClick={() => void onLoadMoreRecents()}
                      // Recents are post-filtered to non-project sessions, so a
                      // backend page size (50) is not a truthful "rows you'll
                      // see" count. Use the generic label instead of a fake N.
                      step={0}
                    />
                  ) : null
                }
                forceEmptyState={showSessionSkeletons}
                groups={displayAgentGroups}
                headerAction={
                  inProject && enteredProject ? (
                    <div className="group/workspace flex shrink-0 items-center gap-0.5">
                      {enteredProject.path && (
                        <StartWorkButton onStarted={onNewSessionInWorkspace} repoPath={enteredProject.path} />
                      )}
                      <ProjectMenu
                        isActive={enteredProject.id === activeProjectId}
                        onExitScope={exitProjectScope}
                        project={enteredProject}
                        scoped
                      />
                      <div className="grid size-6 place-items-center">
                        <Button
                          aria-label={s.showProjects}
                          className={HEADER_NAV_BTN}
                          onClick={event => {
                            event.stopPropagation()
                            exitProjectScope()
                          }}
                          size="icon-xs"
                          variant="ghost"
                        >
                          <Codicon name="list-unordered" size="0.75rem" />
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <div className="flex shrink-0 items-center gap-0.5">
                      {!showAllProfiles ? (
                        <Button
                          aria-label={agentsGrouped ? s.projects.newButton : s.nav['new-session']}
                          className={HEADER_ACTION_BTN}
                          onClick={event => {
                            event.stopPropagation()

                            if (agentsGrouped) {
                              openProjectCreate()
                            } else {
                              onNewSessionInWorkspace(null)
                            }
                          }}
                          size="icon-xs"
                          variant="ghost"
                        >
                          <Codicon name="add" size="0.75rem" />
                        </Button>
                      ) : null}
                      <div className="grid size-6 place-items-center">
                        {!showAllProfiles && agentSessions.length > 0 ? (
                          <Button
                            aria-label={agentsGrouped ? s.showSessions : s.showProjects}
                            className={cn(
                              HEADER_NAV_BTN,
                              agentsGrouped && 'bg-(--ui-control-active-background) text-foreground opacity-100'
                            )}
                            onClick={event => {
                              event.stopPropagation()
                              setSidebarRecentsOpen(true)
                              setSidebarAgentsGrouped(!agentsGrouped)
                            }}
                            size="icon-xs"
                            variant="ghost"
                          >
                            <Codicon name={agentsGrouped ? 'list-unordered' : 'root-folder'} size="0.75rem" />
                          </Button>
                        ) : null}
                      </div>
                    </div>
                  )
                }
                label={sessionsLabel}
                labelMeta={
                  worktreeGroupingActive
                    ? reposScanning
                      ? <Codicon className="text-(--ui-text-quaternary)" name="loading" size="0.6875rem" spinning />
                      : undefined
                    : recentsMeta
                }
                onArchiveSession={onArchiveSession}
                onDeleteSession={onDeleteSession}
                onEnterProject={onEnterProject}
                onNewSessionInWorkspace={showAllProfiles ? undefined : onNewSessionInWorkspace}
                onReorderSessions={showAllProfiles ? undefined : reorderSessions}
                onResumeSession={onResumeSession}
                onToggle={() => setSidebarRecentsOpen(!agentsOpen)}
                onTogglePin={pinSession}
                open={agentsOpen}
                pinned={false}
                projectContent={inProject ? enteredProject : undefined}
                projectLanesLoading={inProject ? scopedLanesLoading : undefined}
                projectOverview={projectOverview}
                projectOverviewPreviews={overviewPreviews}
                projectRepoWorktrees={inProject ? scopedRepoWorktrees : undefined}
                projectsLoading={worktreeGroupingActive ? projectTreeLoading : false}
                rootClassName={cn(
                  'min-h-32 flex-1 overflow-hidden p-0',
                  !recentsVirtualizes && 'compact:min-h-0 compact:flex-none compact:overflow-visible'
                )}
                sessions={displayAgentSessions}
                sortable={!showAllProfiles && agentSessions.length > 1}
                workingSessionIdSet={workingSessionIdSet}
              />
            )}

            {!trimmedQuery &&
              !worktreeGroupingActive &&
              messagingGroups.map(group => {
                const visible = messagingVisible[group.sourceId] ?? NON_SESSION_INITIAL_ROWS
                const shownSessions = group.sessions.slice(0, visible)
                // More to show if rows are hidden behind the cap, or the backend
                // still has older threads on disk.
                const canRevealMore = visible < group.sessions.length || group.hasMore

                return (
                  <SidebarSessionsSection
                    activeSessionId={activeSidebarSessionId}
                    contentClassName={cn('flex max-h-56 flex-col gap-px pb-1.75', GROUP_BODY)}
                    emptyState={null}
                    footer={
                      canRevealMore ? (
                        <SidebarLoadMoreRow
                          loading={Boolean(messagingLoadMorePending[group.sourceId])}
                          onClick={() => revealMoreMessaging(group.sourceId, group.sessions.length, group.hasMore)}
                          step={Math.min(NON_SESSION_LOAD_STEP, Math.max(0, group.total - shownSessions.length))}
                        />
                      ) : null
                    }
                    key={group.sourceId}
                    label={group.label}
                    labelIcon={
                      <PlatformAvatar
                        className="size-4 rounded-[4px] text-[0.5625rem] [&_svg]:size-3"
                        platformId={group.sourceId}
                        platformName={group.label}
                      />
                    }
                    labelMeta={countLabel(group.sessions.length, group.total)}
                    onArchiveSession={onArchiveSession}
                    onDeleteSession={onDeleteSession}
                    onResumeSession={onResumeSession}
                    onToggle={() => toggleSidebarMessagingOpen(group.sourceId)}
                    onTogglePin={pinSession}
                    open={messagingOpenIds.includes(group.sourceId)}
                    pinned={false}
                    rootClassName="shrink-0 p-0"
                    sessions={shownSessions}
                    workingSessionIdSet={workingSessionIdSet}
                  />
                )
              })}

            {!trimmedQuery && !worktreeGroupingActive && cronJobs.length > 0 && (
              <SidebarCronJobsSection
                jobs={cronJobs}
                label={s.cronJobs}
                onManageJob={onManageCronJob}
                onOpenRun={onResumeSession}
                onToggle={() => setSidebarCronOpen(!cronOpen)}
                onTriggerJob={onTriggerCronJob}
                open={cronOpen}
              />
            )}
          </div>
        )}

        {contentVisible && !showSessionSections && <div className="min-h-0 flex-1" />}

        {contentVisible && (
          <div className="shrink-0 px-0.5 pb-1 pt-0.5">
            <ProfileRail />
          </div>
        )}
      </SidebarContent>
      <ProjectDialog />
    </Sidebar>
  )
}

interface SidebarSectionHeaderProps {
  label: string
  open: boolean
  onToggle: () => void
  action?: React.ReactNode
  meta?: React.ReactNode
  icon?: React.ReactNode
  // When false the section can't be collapsed: the label renders static (no
  // toggle, no caret) and the section is always open. Used for the single-
  // project view, where collapsing one project makes no sense.
  collapsible?: boolean
}

function SidebarSectionHeader({ label, open, onToggle, action, meta, icon, collapsible = true }: SidebarSectionHeaderProps) {
  const labelBody = (
    <>
      {icon}
      <SidebarPanelLabel>{label}</SidebarPanelLabel>
      {meta && <SidebarCount>{meta}</SidebarCount>}
    </>
  )

  return (
    <div className="group/section flex shrink-0 items-center justify-between gap-1 pb-1 pt-1.5">
      {collapsible ? (
        <button
          className="group/section-label flex w-fit items-center gap-1 bg-transparent text-left leading-none"
          onClick={onToggle}
          type="button"
        >
          {labelBody}
          <DisclosureCaret
            className="text-(--ui-text-tertiary) opacity-0 transition group-hover/section-label:opacity-100"
            open={open}
          />
        </button>
      ) : (
        <div className="flex w-fit items-center gap-1 leading-none">{labelBody}</div>
      )}
      {action}
    </div>
  )
}

function SidebarSessionSkeletons() {
  return (
    <div aria-hidden="true" className="grid gap-px">
      {['w-32', 'w-40', 'w-28', 'w-36', 'w-24'].map((width, i) => (
        <div className="grid min-h-7 grid-cols-[minmax(0,1fr)_1.5rem] items-center rounded-lg" key={`${width}-${i}`}>
          <Skeleton className={cn('h-3.5 rounded-full', width)} />
          <Skeleton className="mx-auto size-4 rounded-md opacity-60" />
        </div>
      ))}
    </div>
  )
}

// A muted "Loading…" row — used while a project's git worktrees / lane recents
// stream in, so the entered project doesn't read as finished-but-empty.
function SidebarLoadingRow() {
  const { t } = useI18n()

  return (
    <div className="flex min-h-7 items-center gap-1.5 rounded-lg pl-2 text-[0.75rem] text-(--ui-text-tertiary)">
      <Codicon className="shrink-0 text-(--ui-text-quaternary)" name="loading" size="0.75rem" spinning />
      <span>{t.sidebar.loading}</span>
    </div>
  )
}

function SidebarPinnedEmptyState() {
  const { t } = useI18n()

  return (
    <div className="flex min-h-7 items-center gap-1.5 rounded-lg pl-2 text-[0.75rem] text-(--ui-text-tertiary)">
      <span className="grid w-3.5 shrink-0 place-items-center text-(--ui-text-quaternary)">
        <Codicon name="pin" size="0.75rem" />
      </span>
      <span>{t.sidebar.shiftClickHint}</span>
    </div>
  )
}

interface MessagingSection {
  sourceId: string
  label: string
  sessions: SessionInfo[]
  total: number
  hasMore: boolean
}

interface SidebarSessionsSectionProps {
  label: string
  open: boolean
  onToggle: () => void
  sessions: SessionInfo[]
  activeSessionId: null | string
  workingSessionIdSet: Set<string>
  onResumeSession: (sessionId: string) => void
  onDeleteSession: (sessionId: string) => void
  onArchiveSession: (sessionId: string) => void
  onTogglePin: (sessionId: string) => void
  onNewSessionInWorkspace?: (path: null | string) => void
  pinned: boolean
  rootClassName?: string
  contentClassName?: string
  emptyState: React.ReactNode
  forceEmptyState?: boolean
  headerAction?: React.ReactNode
  footer?: React.ReactNode
  groups?: SidebarSessionGroup[]
  tree?: SidebarWorkspaceTree[]
  // Project overview: when present, render a drill-in list of project rows
  // instead of sessions. Clicking a row enters that project (onEnterProject),
  // which then passes `projectContent` on the next render. Takes precedence
  // over `tree` / `groups`.
  projectOverview?: SidebarProjectTree[]
  // Per-project preview rows (from the backend tree), keyed by project path.
  projectOverviewPreviews?: Record<string, SessionInfo[]>
  // True while the backend project tree is loading (overview skeleton).
  projectsLoading?: boolean
  onEnterProject?: (id: string) => void
  // The entered project's flattened content: main-checkout sessions render
  // directly (no redundant repo/branch header); only linked worktrees nest.
  projectContent?: SidebarProjectTree
  // Live git lanes (`git worktree list`) for repos in the entered project —
  // a VISUAL enhancer only (empty lanes), never session membership.
  projectRepoWorktrees?: Record<string, HermesGitWorktree[]>
  // True while the entered project's hydrated lanes are loading.
  projectLanesLoading?: boolean
  activeProjectId?: null | string
  labelMeta?: React.ReactNode
  labelIcon?: React.ReactNode
  // When false the section header is static (no caret/toggle) and always open.
  collapsible?: boolean
  sortable?: boolean
  // The flat session list is the only hand-reorderable surface (grouped/project
  // views sort deterministically), so it owns the one ReorderableList.
  onReorderSessions?: (ids: string[]) => void
  dndSensors?: ReturnType<typeof useSensors>
}

function SidebarSessionsSection({
  label,
  open,
  onToggle,
  sessions,
  activeSessionId,
  workingSessionIdSet,
  onResumeSession,
  onDeleteSession,
  onArchiveSession,
  onTogglePin,
  onNewSessionInWorkspace,
  pinned,
  rootClassName,
  contentClassName,
  emptyState,
  forceEmptyState = false,
  headerAction,
  footer,
  groups,
  projectOverview,
  projectOverviewPreviews,
  projectsLoading = false,
  onEnterProject,
  projectContent,
  projectRepoWorktrees,
  projectLanesLoading = false,
  activeProjectId,
  labelMeta,
  labelIcon,
  collapsible = true,
  sortable = false,
  onReorderSessions,
  dndSensors
}: SidebarSessionsSectionProps) {
  const sectionOpen = collapsible ? open : true
  const hasGroupedSessions = Boolean(groups?.some(group => group.sessions.length > 0))
  // A defined project list is itself content (even an empty project should
  // render as a drill-in row so the user can see it exists).
  const hasProjectOverview = Boolean(projectOverview?.length)
  const hasProjectContent = Boolean(projectContent && projectContent.sessionCount > 0)

  const showEmptyState =
    forceEmptyState ||
    (!hasGroupedSessions && !hasProjectOverview && !hasProjectContent && sessions.length === 0)

  // The flat recents/pinned list is the only place sessions reorder by hand;
  // grouped/tree views always sort by creation date and never drag.
  const sessionsDraggable = sortable && !!onReorderSessions

  const renderRow = (session: SessionInfo, draggable: boolean) => {
    const rowProps = {
      isPinned: pinned,
      isSelected: session.id === activeSessionId,
      isWorking: workingSessionIdSet.has(session.id),
      onArchive: () => onArchiveSession(session.id),
      onDelete: () => onDeleteSession(session.id),
      onPin: () => onTogglePin(sessionPinId(session)),
      onResume: () => onResumeSession(session.id),
      session
    }

    return draggable ? (
      <SortableSidebarSessionRow key={session.id} {...rowProps} />
    ) : (
      <SidebarSessionRow key={session.id} {...rowProps} />
    )
  }

  // Sessions inside repos/worktrees are date-ordered and static.
  const renderRows = (items: SessionInfo[]) => items.map(session => renderRow(session, false))

  const flatVirtualized =
    !showEmptyState &&
    !groups?.length &&
    !projectOverview?.length &&
    !projectContent &&
    sessions.length >= VIRTUALIZE_THRESHOLD

  let inner: React.ReactNode

  if (showEmptyState) {
    // While the backend project tree is still loading, show skeletons instead of
    // the "no sessions" empty state so the overview doesn't flash empty.
    inner = projectsLoading ? <SidebarSessionSkeletons /> : emptyState
  } else if (projectContent) {
    inner = (
      <EnteredProjectContent
        lanesLoading={projectLanesLoading}
        onNewSession={onNewSessionInWorkspace}
        project={projectContent}
        renderRows={renderRows}
        repoWorktrees={projectRepoWorktrees}
      />
    )
  } else if (projectOverview?.length) {
    const explicit = projectOverview.filter(project => !project.isAuto)
    const auto = projectOverview.filter(project => project.isAuto)

    // Saved projects first, discovered repos second — ordering carries the
    // distinction; no section labels.
    inner = [...explicit, ...auto].map(project => (
      <ProjectOverviewRow
        activeProjectId={activeProjectId}
        key={project.id}
        onEnter={onEnterProject}
        onNewSession={onNewSessionInWorkspace}
        previewSessions={project.path ? projectOverviewPreviews?.[project.path] : undefined}
        project={project}
        renderRows={renderRows}
      />
    ))
  } else if (groups?.length) {
    // Profile/source groups never reorder; render them flat with static rows.
    inner = groups.map(group => (
      <SidebarWorkspaceGroup group={group} key={group.id} onNewSession={onNewSessionInWorkspace} renderRows={renderRows} />
    ))
  } else if (flatVirtualized) {
    const virtual = (
      <VirtualSessionList
        activeSessionId={activeSessionId}
        className={contentClassName}
        onArchiveSession={onArchiveSession}
        onDeleteSession={onDeleteSession}
        onResumeSession={onResumeSession}
        onTogglePin={onTogglePin}
        pinned={pinned}
        sessions={sessions}
        sortable={sessionsDraggable}
        workingSessionIdSet={workingSessionIdSet}
      />
    )

    inner =
      sessionsDraggable && onReorderSessions ? (
        <ReorderableList ids={sessions.map(s => s.id)} onReorder={onReorderSessions} sensors={dndSensors}>
          {virtual}
        </ReorderableList>
      ) : (
        virtual
      )
  } else if (sessionsDraggable && onReorderSessions) {
    inner = (
      <ReorderableList ids={sessions.map(s => s.id)} onReorder={onReorderSessions} sensors={dndSensors}>
        {sessions.map(session => renderRow(session, true))}
      </ReorderableList>
    )
  } else {
    inner = renderRows(sessions)
  }

  // The virtualizer owns its own scroller, so suppress the wrapper's overflow
  // to avoid a double scroll container.
  const resolvedContentClassName = cn(contentClassName, flatVirtualized && 'overflow-y-visible')

  return (
    <SidebarGroup className={rootClassName}>
      <SidebarSectionHeader
        action={headerAction}
        collapsible={collapsible}
        icon={labelIcon}
        label={label}
        meta={labelMeta}
        onToggle={onToggle}
        open={sectionOpen}
      />
      {sectionOpen && (
        <SidebarGroupContent className={resolvedContentClassName}>
          {inner}
          {footer}
        </SidebarGroupContent>
      )}
    </SidebarGroup>
  )
}

interface SidebarWorkspaceGroupProps {
  group: SidebarSessionGroup
  renderRows: (sessions: SessionInfo[]) => React.ReactNode
  onNewSession?: (path: null | string) => void
  // When set (linked worktree rows), shows a remove affordance that runs a real
  // `git worktree remove`.
  onRemove?: () => void
}

function SidebarWorkspaceGroup({ group, renderRows, onNewSession, onRemove }: SidebarWorkspaceGroupProps) {
  const { t } = useI18n()
  const s = t.sidebar
  const isProfileGroup = group.mode === 'profile'
  const pageStep = SIDEBAR_GROUP_PAGE
  const [open, toggleOpen] = useWorkspaceNodeOpen(group.id)
  const [visibleCount, setVisibleCount] = useState(pageStep)

  const loadedCount = group.sessions.length
  // Profile groups know their on-disk total (children excluded); workspace
  // groups only ever page within what's already loaded.
  const totalCount = isProfileGroup ? Math.max(group.totalCount ?? loadedCount, loadedCount) : loadedCount
  const visibleSessions = group.sessions.slice(0, visibleCount)
  const hiddenCount = Math.max(0, totalCount - visibleSessions.length)
  const nextCount = Math.min(pageStep, hiddenCount)

  // Leading glyph: profile color dot, or a branch/kanban mark for a worktree.
  const leadingIcon = group.color ? (
    <span aria-hidden="true" className="size-2 shrink-0 rounded-full" style={{ backgroundColor: group.color }} />
  ) : (
    <Codicon className="shrink-0 text-(--ui-text-tertiary)" name={group.isKanban ? 'checklist' : 'git-branch'} size="0.75rem" />
  )

  // Reveal already-loaded rows first; only hit the backend when the next page
  // crosses what's been fetched for this profile.
  const handleProfileLoadMore = () => {
    const target = visibleCount + pageStep

    setVisibleCount(target)

    if (target > loadedCount && loadedCount < totalCount) {
      group.onLoadMore?.()
    }
  }

  return (
    <div className={SIDEBAR_STACK}>
      <WorkspaceHeader
        action={
          (onNewSession || isProfileGroup || onRemove) && (
            <div className="flex items-center">
              {(onNewSession || isProfileGroup) && (
                <WorkspaceAddButton
                  label={s.newSessionIn(group.label)}
                  // Profile groups start a fresh session in that profile but keep
                  // the all-profiles browse view (newSessionInProfile leaves the
                  // scope alone); workspace groups seed the new session's cwd.
                  onClick={() => (isProfileGroup ? newSessionInProfile(group.id) : onNewSession?.(group.path))}
                />
              )}
              {onRemove && (
                <button
                  aria-label={s.projects.removeWorktree}
                  className="grid size-4 shrink-0 place-items-center rounded-sm bg-transparent text-(--ui-text-quaternary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-destructive group-hover/workspace:opacity-100"
                  onClick={event => {
                    event.stopPropagation()
                    onRemove()
                  }}
                  type="button"
                >
                  <Codicon name="trash" size="0.75rem" />
                </button>
              )}
            </div>
          )
        }
        count={isProfileGroup ? countLabel(visibleSessions.length, totalCount) : group.sessions.length}
        icon={leadingIcon}
        label={group.label}
        onToggle={toggleOpen}
        open={open}
      />
      {open && (
        <>
          {visibleSessions.length === 0 ? (
            <div className="min-h-7 pl-2 text-[0.75rem] leading-7 text-(--ui-text-quaternary)">{s.noSessions}</div>
          ) : (
            renderRows(visibleSessions)
          )}
          {hiddenCount > 0 &&
            (isProfileGroup ? (
              <SidebarLoadMoreRow
                loading={Boolean(group.loadingMore)}
                onClick={handleProfileLoadMore}
                step={nextCount}
              />
            ) : (
              <WorkspaceShowMoreButton
                count={nextCount}
                label={group.label}
                onClick={() => setVisibleCount(count => count + SIDEBAR_GROUP_PAGE)}
              />
            ))}
        </>
      )}
    </div>
  )
}

// Leading glyph shared by the overview row + scope banner.
function projectIcon(project: SidebarProjectTree) {
  if (project.color) {
    return <span aria-hidden="true" className="size-2 shrink-0 rounded-full" style={{ backgroundColor: project.color }} />
  }

  return <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="folder-library" size="0.75rem" />
}

interface ProjectOverviewRowProps {
  project: SidebarProjectTree
  onEnter?: (id: string) => void
  onNewSession?: (path: null | string) => void
  renderRows?: (sessions: SessionInfo[]) => React.ReactNode
  activeProjectId?: null | string
  // Recents fetched by cwd-prefix for this project (self-managed), used when the
  // loaded recents page doesn't already contain the project's sessions.
  previewSessions?: SessionInfo[]
}

// Number of recent sessions previewed under each project in the overview.
const PROJECT_PREVIEW_COUNT = 3

// One row in the project overview: icon + name (click to enter), a new-session +
// (reveal on hover), and the manage menu (⋮). Below it, a preview of the
// project's most recent sessions — clickable to resume without entering.
function ProjectOverviewRow({
  project,
  onEnter,
  onNewSession,
  renderRows,
  activeProjectId,
  previewSessions
}: ProjectOverviewRowProps) {
  const { t } = useI18n()
  const s = t.sidebar
  const isActive = project.id === activeProjectId
  // Prefer the project's own cwd-prefix fetch (authoritative recents, including
  // sessions off the loaded page); fall back to whatever's already loaded.
  const fetched = (previewSessions ?? []).slice(0, PROJECT_PREVIEW_COUNT)
  const preview = renderRows ? (fetched.length ? fetched : latestProjectSessions(project, PROJECT_PREVIEW_COUNT)) : []

  return (
    <div>
      <div className="group/workspace flex min-h-7 items-center gap-1 rounded-md pl-2 pr-1 hover:bg-(--ui-control-hover-background)">
        <button
          aria-label={s.projects.enter(project.label)}
          className="flex min-w-0 flex-1 items-center gap-1.5 bg-transparent py-1 text-left"
          onClick={() => onEnter?.(project.id)}
          type="button"
        >
          {projectIcon(project)}
          <span
            className={cn(
              'min-w-0 flex-1 truncate text-[0.8125rem] text-(--ui-text-secondary)',
              isActive && 'font-medium text-foreground'
            )}
          >
            {project.label}
          </span>
        </button>
        {onNewSession && (
          <WorkspaceAddButton label={s.newSessionIn(project.label)} onClick={() => onNewSession(project.path)} />
        )}
        <ProjectMenu isActive={isActive} project={project} />
      </div>
      {preview.length > 0 && (
        <div className={cn(SIDEBAR_STACK, 'pb-1 pl-4')}>{renderRows?.(preview)}</div>
      )}
    </div>
  )
}

// The entered project's body. Main-checkout sessions render directly — no
// redundant repo/branch header (the breadcrumb already names the project). Only
// linked worktrees nest, shown by branch. Multi-folder projects keep per-repo
// headers so the folders stay distinguishable.
function EnteredProjectContent({
  project,
  renderRows,
  onNewSession,
  lanesLoading = false,
  repoWorktrees
}: {
  project: SidebarProjectTree
  renderRows: (sessions: SessionInfo[]) => React.ReactNode
  onNewSession?: (path: null | string) => void
  lanesLoading?: boolean
  repoWorktrees?: Record<string, HermesGitWorktree[]>
}) {
  if (!project.repos.length) {
    return null
  }

  const single = project.repos.length === 1

  return (
    <>
      {project.repos.map(repo => (
        <RepoFlatSection
          discoveredWorktrees={repo.path ? repoWorktrees?.[repo.path] : undefined}
          key={repo.id}
          onNewSession={onNewSession}
          renderRows={renderRows}
          repo={repo}
          showHeader={!single}
        />
      ))}
      {lanesLoading && <SidebarLoadingRow />}
    </>
  )
}

function RepoFlatSection({
  repo,
  showHeader,
  renderRows,
  onNewSession,
  discoveredWorktrees
}: {
  repo: SidebarWorkspaceTree
  showHeader: boolean
  renderRows: (sessions: SessionInfo[]) => React.ReactNode
  onNewSession?: (path: null | string) => void
  discoveredWorktrees?: HermesGitWorktree[]
}) {
  const { t } = useI18n()
  const s = t.sidebar
  const [open, toggleOpen] = useWorkspaceNodeOpen(repo.id)
  const dismissedWorktrees = useStore($dismissedWorktreeIds)

  // The repo's session lanes already come fully built from the backend; this
  // only injects empty VISUAL lanes from a live `git worktree list`.
  const mergedGroups = useMemo(
    () => mergeRepoWorktreeGroups(repo, discoveredWorktrees),
    [repo, discoveredWorktrees]
  )

  // Main lanes are always visible; linked worktrees can be user-dismissed.
  const ordered = mergedGroups.filter(group => group.isMain || !dismissedWorktrees.includes(group.id))
  const repoCount = ordered.reduce((sum, group) => sum + group.sessions.length, 0)

  const removeWorktree = async (group: SidebarSessionGroup) => {
    if (!repo.path || !group.path) {
      return
    }

    try {
      await removeWorktreePath(repo.path, group.path)
      dismissWorktree(group.id)
    } catch (err) {
      notifyError(err, s.projects.removeWorktreeFailed)
    }
  }

  const body = (
    <>
      {ordered.map(group => (
        <SidebarWorkspaceGroup
          group={group}
          key={group.id}
          // The kanban bucket is read-only: it aggregates many task worktrees, so
          // "new session here" and "remove worktree" have no single target.
          onNewSession={group.isKanban ? undefined : onNewSession}
          onRemove={group.isMain || group.isKanban ? undefined : () => void removeWorktree(group)}
          renderRows={renderRows}
        />
      ))}
    </>
  )

  if (!showHeader) {
    return body
  }

  return (
    <div className={SIDEBAR_STACK}>
      <WorkspaceHeader
        action={
          onNewSession && <WorkspaceAddButton label={s.newSessionIn(repo.label)} onClick={() => onNewSession(repo.path)} />
        }
        count={repoCount}
        emphasis
        icon={<Codicon className="shrink-0 text-(--ui-text-tertiary)" name="repo" size="0.75rem" />}
        label={repo.label}
        onToggle={toggleOpen}
        open={open}
      />
      {open && <div className={cn(SIDEBAR_STACK, 'pl-2.5')}>{body}</div>}
    </div>
  )
}

// Per-project actions, modeled on git GUIs (GitHub Desktop / GitKraken): reveal
// in the file manager, copy path, and "Remove from sidebar" (never deletes files
// — auto projects are dismissed, explicit ones drop their entry). Explicit
// projects additionally get rename / add folder / set active. Hidden until the
// row is hovered (group/workspace), matching the + affordance.
function ProjectMenu({
  project,
  isActive,
  scoped = false,
  onExitScope
}: {
  project: SidebarProjectTree
  isActive: boolean
  // True when rendered in the entered-project header, so removal can leave the
  // now-defunct scope.
  scoped?: boolean
  onExitScope?: () => void
}) {
  const { t } = useI18n()
  const p = t.sidebar.projects
  const target = { id: project.id, name: project.label }
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false)

  const removeAuto = () => {
    dismissAutoProject(project.id)

    if (scoped) {
      onExitScope?.()
    }
  }

  const confirmDelete = () => {
    void deleteProject(project.id)
    setConfirmDeleteOpen(false)

    if (scoped) {
      onExitScope?.()
    }
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            aria-label={p.menu}
            className={cn(
              'grid size-4 shrink-0 place-items-center rounded-sm bg-transparent text-(--ui-text-quaternary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground data-[state=open]:opacity-100',
              // In the project header reveal on the whole header hover; in overview
              // rows reveal on the row hover.
              scoped ? 'group-hover/section:opacity-100' : 'group-hover/workspace:opacity-100'
            )}
            onClick={event => event.stopPropagation()}
            type="button"
          >
            <Codicon name="kebab-vertical" size="0.75rem" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-48" sideOffset={6}>
          {!project.isAuto && (
            <>
              <DropdownMenuItem onSelect={() => openProjectRename(target)}>
                <Codicon name="edit" size="0.875rem" />
                <span>{p.menuRename}</span>
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => openProjectAddFolder(target)}>
                <Codicon name="new-folder" size="0.875rem" />
                <span>{p.menuAddFolder}</span>
              </DropdownMenuItem>
              <DropdownMenuItem disabled={isActive} onSelect={() => void setActiveProject(project.id)}>
                <Codicon name="target" size="0.875rem" />
                <span>{p.menuSetActive}</span>
              </DropdownMenuItem>
              <DropdownMenuSeparator />
            </>
          )}
          <DropdownMenuItem disabled={!project.path} onSelect={() => void revealPath(project.path)}>
            <Codicon name="folder-opened" size="0.875rem" />
            <span>{p.reveal}</span>
          </DropdownMenuItem>
          <DropdownMenuItem disabled={!project.path} onSelect={() => void copyPath(project.path)}>
            <Codicon name="copy" size="0.875rem" />
            <span>{p.copyPath}</span>
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          {project.isAuto ? (
            <DropdownMenuItem onSelect={removeAuto} variant="destructive">
              <Codicon name="trash" size="0.875rem" />
              <span>{p.removeFromSidebar}</span>
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem onSelect={() => setConfirmDeleteOpen(true)} variant="destructive">
              <Codicon name="trash" size="0.875rem" />
              <span>{`${p.menuDelete}…`}</span>
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      <Dialog onOpenChange={setConfirmDeleteOpen} open={confirmDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{`${p.menuDelete} "${project.label}"?`}</DialogTitle>
            <DialogDescription>
              This removes the saved project from Hermes. Files, git repos, and worktrees stay untouched.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button onClick={() => setConfirmDeleteOpen(false)} variant="ghost">
              Cancel
            </Button>
            <Button onClick={confirmDelete} variant="destructive">
              {p.menuDelete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

function SidebarCount({ children }: { children: React.ReactNode }) {
  return <span className="text-[0.6875rem] font-medium text-(--ui-text-quaternary)">{children}</span>
}

// Reveals the next page of already-loaded rows within a workspace/worktree.
function WorkspaceShowMoreButton({ count, label, onClick }: { count: number; label: string; onClick: () => void }) {
  const { t } = useI18n()
  const text = t.sidebar.showMoreIn(count, label)

  return (
    <button
      aria-label={text}
      className="ml-auto grid size-5 place-items-center rounded-sm bg-transparent text-(--ui-text-tertiary) transition-colors hover:bg-(--ui-control-hover-background) hover:text-foreground"
      onClick={onClick}
      type="button"
    >
      <Codicon name="ellipsis" size="0.75rem" />
    </button>
  )
}

// "+" affordance shared by repo and worktree headers — reveals on header hover.
function WorkspaceAddButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      aria-label={label}
      className="grid size-4 shrink-0 place-items-center rounded-sm bg-transparent text-(--ui-text-quaternary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground group-hover/workspace:opacity-100"
      onClick={onClick}
      type="button"
    >
      <Codicon name="add" size="0.75rem" />
    </button>
  )
}

// "New worktree": prompt for a branch name, then git spins up a fresh worktree
// for that branch under the repo (the lightest way) and we open a new session
// inside it. Naming is explicit — no auto-generated `hermes/work-<ts>` trees.
function StartWorkButton({ repoPath, onStarted }: { repoPath: string; onStarted: (path: string) => void }) {
  const { t } = useI18n()
  const s = t.sidebar
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [pending, setPending] = useState(false)

  const submit = async () => {
    const branch = name.trim()

    if (pending || !repoPath || !branch) {
      return
    }

    setPending(true)

    try {
      // Pass the typed value as both the dir slug source and the branch, so the
      // branch is exactly what the user named (the dir is slugified git-side).
      const result = await startWorkInRepo(repoPath, { branch, name: branch })

      if (result) {
        onStarted(result.path)
        setOpen(false)
        setName('')
      }
    } catch (err) {
      notifyError(err, s.projects.startWorkFailed)
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <button
        aria-label={s.projects.startWork}
        className="grid size-4 shrink-0 place-items-center rounded-sm bg-transparent text-(--ui-text-quaternary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground group-hover/section:opacity-100 focus-visible:opacity-100"
        onClick={() => setOpen(true)}
        type="button"
      >
        <Codicon name="git-branch" size="0.75rem" />
      </button>
      <Dialog onOpenChange={setOpen} open={open}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{s.projects.newWorktreeTitle}</DialogTitle>
            <DialogDescription>{s.projects.newWorktreeDesc}</DialogDescription>
          </DialogHeader>
          <Input
            autoFocus
            disabled={pending}
            onChange={event => setName(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void submit()
              } else if (event.key === 'Escape') {
                setOpen(false)
              }
            }}
            placeholder={s.projects.branchPlaceholder}
            value={name}
          />
          <DialogFooter>
            <Button disabled={pending} onClick={() => setOpen(false)} type="button" variant="ghost">
              {t.common.cancel}
            </Button>
            <Button disabled={pending || !name.trim()} onClick={() => void submit()} type="button">
              {s.projects.startWork}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

// Collapsible header shared by the repo (emphasis) and worktree levels: a
// toggle button with a leading glyph, plus an optional trailing action (the +).
function WorkspaceHeader({
  action,
  count,
  emphasis = false,
  icon,
  label,
  onToggle,
  open
}: {
  action?: React.ReactNode
  count: React.ReactNode
  emphasis?: boolean
  icon: React.ReactNode
  label: string
  onToggle: () => void
  open: boolean
}) {
  return (
    <div
      className={cn(
        'group/workspace flex min-h-6 items-center gap-1 px-2 pt-1 text-[0.6875rem]',
        emphasis ? 'font-semibold text-(--ui-text-secondary)' : 'font-medium text-(--ui-text-tertiary)'
      )}
    >
      <button
        className={cn(
          'flex min-w-0 flex-1 items-center gap-1.5 bg-transparent text-left',
          emphasis ? 'hover:text-foreground' : 'hover:text-(--ui-text-secondary)'
        )}
        onClick={onToggle}
        type="button"
      >
        {icon}
        <span className="min-w-0 truncate">{label}</span>
        <span className="shrink-0">
          <SidebarCount>{count}</SidebarCount>
        </span>
        <DisclosureCaret
          className="shrink-0 text-(--ui-text-tertiary) opacity-0 transition group-hover/workspace:opacity-100"
          open={open}
        />
      </button>
      {action}
    </div>
  )
}

interface SortableSessionRowProps {
  session: SessionInfo
  isPinned: boolean
  isSelected: boolean
  isWorking: boolean
  onArchive: () => void
  onDelete: () => void
  onPin: () => void
  onResume: () => void
}

function SortableSidebarSessionRow(props: SortableSessionRowProps) {
  return <SidebarSessionRow {...props} {...useSortableBindings(props.session.id)} />
}
