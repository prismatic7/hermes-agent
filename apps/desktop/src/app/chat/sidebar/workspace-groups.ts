import type { HermesGitWorktree } from '@/global'
import type { ProjectInfo, SessionInfo } from '@/hermes'

// Session grouping is now computed authoritatively on the backend
// (`tui_gateway/project_tree.py`, exposed via `projects.tree` /
// `projects.project_sessions`). The desktop is a thin renderer: this module
// only holds the render contract (the three tree interfaces) plus a couple of
// pure helpers and the VISUAL-ONLY worktree enhancer that injects empty lanes
// from `git worktree list`. It never decides session membership.

export interface SidebarSessionGroup {
  id: string
  label: string
  path: null | string
  sessions: SessionInfo[]
  // Profile color for the ALL-profiles view; absent for workspace groups.
  color?: null | string
  // True when this group is a repo's main checkout (vs a linked worktree).
  isMain?: boolean
  // True for the synthetic lane that collapses all of a repo's kanban task
  // worktrees (`<repo>/.worktrees/t_*`) into one row, so a heavy board doesn't
  // spray hundreds of throwaway branch lanes across the sidebar.
  isKanban?: boolean
  loadingMore?: boolean
  mode?: 'profile' | 'source' | 'workspace'
  onLoadMore?: () => void
  sourceId?: string
  totalCount?: number
}

/** A repo node: holds its branch/worktree lanes (`repo -> lane -> sessions`). */
export interface SidebarWorkspaceTree {
  id: string
  label: string
  path: null | string
  groups: SidebarSessionGroup[]
  sessionCount: number
}

/** A project node: human-named (or repo-derived), holds its repo subtree. */
export interface SidebarProjectTree {
  id: string
  label: string
  path: null | string
  color?: null | string
  icon?: null | string
  archived?: boolean
  // A git repo root promoted automatically (not a user-created projects.db row).
  // Deletable = dismissable.
  isAuto?: boolean
  // The synthetic "No project" bucket for cwd-less sessions.
  isNoProject?: boolean
  repos: SidebarWorkspaceTree[]
  sessionCount: number
  // Max activity timestamp across the project's sessions (overview sort key).
  lastActive?: number
  // Up to N most-recent sessions for the overview preview (set by `projects.tree`).
  previewSessions?: SessionInfo[]
}

/** Path split into segments, ignoring trailing slashes and mixed separators. */
const segments = (path: string): string[] => path.replace(/[/\\]+$/, '').split(/[/\\]/).filter(Boolean)

/** Last path segment. */
export const baseName = (path: string): string | undefined => segments(path).pop()

// The `.worktrees` dir for a KANBAN-TASK worktree path, else null. Only matches
// task worktrees (`<repo>/.worktrees/t_<hex>`, the `t_…` id kanban_db mints) so
// the many ephemeral task worktrees collapse into one lane — while user-named
// "New worktree" dirs (`<repo>/.worktrees/<slug>`) stay as their own lanes.
const KANBAN_DIR_RE = /^(.*[/\\]\.worktrees)[/\\]t_[0-9a-f]+[/\\]?$/

export function kanbanWorktreeDir(path: string): null | string {
  return path.match(KANBAN_DIR_RE)?.[1] ?? null
}

/** Label for a main-checkout lane whose session recorded no branch. */
export const DEFAULT_BRANCH_LABEL = 'main'

/** The one definition of a main-checkout lane id (must match the backend tree). */
export const branchLaneId = (repoRoot: string, branch?: string): string => `${repoRoot}::branch::${(branch ?? '').trim()}`

/** Default-branch names that sort first and read as the repo's trunk. */
const TRUNK_BRANCHES = new Set(['main', 'master', 'trunk', 'develop'])

function compareWorktreeGroups(a: SidebarSessionGroup, b: SidebarSessionGroup): number {
  if (Boolean(a.isMain) !== Boolean(b.isMain)) {
    return a.isMain ? -1 : 1
  }

  if (a.isMain && b.isMain) {
    const aTrunk = TRUNK_BRANCHES.has(a.label.toLowerCase())
    const bTrunk = TRUNK_BRANCHES.has(b.label.toLowerCase())

    if (aTrunk !== bTrunk) {
      return aTrunk ? -1 : 1
    }
  }

  // The collapsed kanban bucket sinks below real branches.
  if (Boolean(a.isKanban) !== Boolean(b.isKanban)) {
    return a.isKanban ? 1 : -1
  }

  return a.label.localeCompare(b.label, undefined, { sensitivity: 'base' })
}

export function sortWorktreeGroups(groups: SidebarSessionGroup[]): SidebarSessionGroup[] {
  return [...groups].sort(compareWorktreeGroups)
}

/**
 * VISUAL enhancer only: inject empty lanes from a live `git worktree list` so a
 * repo shows its branches/worktrees even when they have no Hermes sessions yet.
 * The repo's real session lanes already come fully built from the backend
 * (`projects.project_sessions`); this never adds or moves session rows, and it
 * degrades to a no-op on remote backends (where the Electron probe returns
 * nothing). Lanes already present (by id/path) are left untouched.
 */
export function mergeRepoWorktreeGroups(
  repo: Pick<SidebarWorkspaceTree, 'groups' | 'id' | 'path'>,
  discoveredWorktrees?: HermesGitWorktree[]
): SidebarSessionGroup[] {
  const merged = [...repo.groups]
  const seenIds = new Set(merged.map(group => group.id))
  const seenPaths = new Set(merged.map(group => group.path).filter((path): path is string => Boolean(path)))
  // Dedupe by branch label too: a branch shows once even if it's checked out in
  // a linked worktree AND already has a session lane (e.g. a worktree sitting on
  // `main` must not spawn a second, empty "main" next to the trunk lane).
  const seenLabels = new Set(merged.map(group => group.label.toLowerCase()))

  for (const worktree of discoveredWorktrees ?? []) {
    const wtPath = worktree.path?.trim()

    if (!wtPath) {
      continue
    }

    // Kanban task worktrees never get their own lane — they fold into the
    // session-derived `::kanban` bucket. Listing every `git worktree list` entry
    // here is exactly what blew the sidebar up to hundreds of empty rows.
    if (!worktree.isMain && kanbanWorktreeDir(wtPath)) {
      continue
    }

    const label = (worktree.isMain ? worktree.branch?.trim() || DEFAULT_BRANCH_LABEL : worktree.branch?.trim()) || baseName(wtPath) || wtPath
    const id = worktree.isMain ? branchLaneId(repo.id, label) : wtPath

    if (seenIds.has(id) || seenPaths.has(wtPath) || seenLabels.has(label.toLowerCase())) {
      continue
    }

    merged.push({ id, isMain: worktree.isMain, label, path: wtPath, sessions: [] })
    seenIds.add(id)
    seenPaths.add(wtPath)
    seenLabels.add(label.toLowerCase())
  }

  return sortWorktreeGroups(merged)
}

// ── Live session overlay ─────────────────────────────────────────────────────
// The backend tree is a snapshot (sessions with >=1 message, refreshed on a
// turn boundary). For parity with the flat Recents list — instant insertion of
// a freshly-created session and the live "working" arc — we overlay the live
// `$sessions` store onto the tree at render time. This is ADDITIVE only: the
// backend still owns membership, structure, counts, and history. The overlay
// just places rows already present in `$sessions` into the project/lane the
// backend would put them in, using the same id scheme. Worktree/kanban folding
// needs the backend common-root probe, so those rows are left for the next
// tree refresh; the common case (a new main-checkout session) overlays here.

export const sessionRecency = (session: SessionInfo): number => session.last_active || session.started_at || 0

/** True when `target` equals `folder` or is nested under it (segment-wise). */
function isPathUnder(folder: string, target: string): boolean {
  const f = segments(folder)
  const t = segments(target)

  if (!f.length || f.length > t.length) {
    return false
  }

  return f.every((seg, i) => seg === t[i])
}

interface LiveLanePlacement {
  projectId: string
  repoRoot: string
  laneId: string
  laneLabel: string
  lanePath: string
}

/** Where a live session overlays: its project id + main-checkout lane key. */
export function placeLiveSession(session: SessionInfo, explicitProjects: ProjectInfo[]): LiveLanePlacement | null {
  const cwd = (session.cwd || '').trim()

  if (!cwd || kanbanWorktreeDir(cwd)) {
    return null
  }

  // No persisted repo root yet (brand-new session) → the cwd is the root.
  const repoRoot = (session.git_repo_root || '').trim() || cwd
  const underRepo = cwd === repoRoot || cwd.startsWith(`${repoRoot}/`) || cwd.startsWith(`${repoRoot}\\`)

  // Linked worktrees (cwd outside the repo root) need backend folding — skip.
  if (!underRepo || cwd.startsWith(`${repoRoot}/.worktrees/`) || cwd.startsWith(`${repoRoot}\\.worktrees\\`)) {
    return null
  }

  let projectId = ''
  let bestLen = -1

  for (const project of explicitProjects) {
    if (project.archived) {
      continue
    }

    for (const folder of project.folders) {
      if (isPathUnder(folder.path, cwd) || isPathUnder(folder.path, repoRoot)) {
        const len = segments(folder.path).length

        if (len > bestLen) {
          bestLen = len
          projectId = project.id
        }
      }
    }
  }

  // Auto projects are keyed by their repo root (matches the backend tree id).
  if (!projectId) {
    projectId = repoRoot
  }

  // Empty branch folds into the one trunk "main" lane (matches the backend), so
  // overlaying never spawns a second "main".
  const branch = (session.git_branch || '').trim() || DEFAULT_BRANCH_LABEL

  return {
    projectId,
    repoRoot,
    laneId: branchLaneId(repoRoot, branch),
    laneLabel: branch,
    lanePath: repoRoot
  }
}

const upsertSession = (rows: SessionInfo[], session: SessionInfo): SessionInfo[] =>
  [session, ...rows.filter(row => row.id !== session.id)].sort((a, b) => b.started_at - a.started_at)

/** Overlay live sessions into an entered project's lanes (instant + working state). */
export function overlayLiveLanes(
  project: SidebarProjectTree,
  live: SessionInfo[],
  explicitProjects: ProjectInfo[]
): SidebarProjectTree {
  const mine = live
    .map(session => ({ session, placement: placeLiveSession(session, explicitProjects) }))
    .filter((entry): entry is { session: SessionInfo; placement: LiveLanePlacement } =>
      Boolean(entry.placement && entry.placement.projectId === project.id)
    )

  if (!mine.length) {
    return project
  }

  const single = project.repos.length <= 1

  const repos = project.repos.map(repo => {
    const lanes = repo.groups.map(group => ({ ...group, sessions: [...group.sessions] }))

    for (const { session, placement } of mine) {
      if (!single && repo.id !== placement.repoRoot) {
        continue
      }

      let lane =
        lanes.find(group => group.id === placement.laneId) ??
        lanes.find(group => group.isMain && group.label.toLowerCase() === placement.laneLabel.toLowerCase())

      if (!lane) {
        lane = { id: placement.laneId, isMain: true, label: placement.laneLabel, path: placement.lanePath, sessions: [] }
        lanes.push(lane)
      }

      lane.sessions = upsertSession(lane.sessions, session)
    }

    return { ...repo, groups: sortWorktreeGroups(lanes), sessionCount: lanes.reduce((n, group) => n + group.sessions.length, 0) }
  })

  return { ...project, repos, sessionCount: repos.reduce((n, repo) => n + repo.sessionCount, 0) }
}

/** Merge live sessions into per-project overview previews, keyed by project path. */
export function overlayLivePreviews(
  projects: SidebarProjectTree[],
  live: SessionInfo[],
  explicitProjects: ProjectInfo[],
  limit: number
): Record<string, SessionInfo[]> {
  const byProject = new Map<string, SessionInfo[]>()

  for (const session of live) {
    const placement = placeLiveSession(session, explicitProjects)

    if (!placement) {
      continue
    }

    const arr = byProject.get(placement.projectId) ?? []
    arr.push(session)
    byProject.set(placement.projectId, arr)
  }

  const out: Record<string, SessionInfo[]> = {}

  for (const node of projects) {
    if (!node.path) {
      continue
    }

    const liveRows = byProject.get(node.id) ?? []
    const base = node.previewSessions ?? []

    if (!liveRows.length && !base.length) {
      continue
    }

    // Live rows take precedence (fresher title/activity/working state).
    const map = new Map<string, SessionInfo>()

    for (const session of [...liveRows, ...base]) {
      if (!map.has(session.id)) {
        map.set(session.id, session)
      }
    }

    out[node.path] = [...map.values()].sort((a, b) => sessionRecency(b) - sessionRecency(a)).slice(0, limit)
  }

  return out
}
