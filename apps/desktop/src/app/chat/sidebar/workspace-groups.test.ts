import { describe, expect, it } from 'vitest'

import type { HermesGitWorktree } from '@/global'
import type { SessionInfo } from '@/types/hermes'

import type { ProjectInfo } from '@/types/hermes'

import {
  baseName,
  kanbanWorktreeDir,
  mergeRepoWorktreeGroups,
  overlayLiveLanes,
  overlayLivePreviews,
  placeLiveSession,
  type SidebarProjectTree,
  type SidebarSessionGroup,
  sortWorktreeGroups
} from './workspace-groups'

// The grouping itself now lives on the backend (tui_gateway/project_tree.py,
// covered by tests/tui_gateway/test_project_tree.py). This file only covers the
// thin render helpers the desktop still owns + the VISUAL worktree enhancer.

let nextId = 0

function makeSession(cwd: null | string, overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    archived: false,
    cwd,
    ended_at: null,
    id: `s${nextId++}`,
    input_tokens: 0,
    is_active: false,
    last_active: 1_000,
    message_count: 1,
    model: 'claude',
    output_tokens: 0,
    preview: null,
    source: 'cli',
    started_at: 1_000,
    title: null,
    tool_call_count: 0,
    ...overrides
  }
}

const lane = (over: Partial<SidebarSessionGroup> & Pick<SidebarSessionGroup, 'id' | 'label'>): SidebarSessionGroup => ({
  path: null,
  sessions: [],
  ...over
})

describe('baseName', () => {
  it('returns the final path segment, ignoring trailing slashes and separators', () => {
    expect(baseName('/www/hermes-agent/')).toBe('hermes-agent')
    expect(baseName('C:\\repos\\app')).toBe('app')
    expect(baseName('')).toBeUndefined()
  })
})

describe('kanbanWorktreeDir', () => {
  it('matches a kanban task worktree (t_<hex>) and returns its .worktrees dir', () => {
    expect(kanbanWorktreeDir('/repo/.worktrees/t_aaaaaaaa')).toBe('/repo/.worktrees')
  })

  it('does NOT match a user-named "New worktree" under .worktrees/ (its own lane)', () => {
    expect(kanbanWorktreeDir('/repo/.worktrees/test-gui-stuff')).toBeNull()
  })

  it('returns null for non-kanban paths', () => {
    expect(kanbanWorktreeDir('/repo/src')).toBeNull()
    expect(kanbanWorktreeDir('/repo')).toBeNull()
  })
})

describe('sortWorktreeGroups', () => {
  it('orders main branches first (trunk ahead of features), kanban last, then alpha', () => {
    const groups = [
      lane({ id: 'k', label: 'kanban', isKanban: true }),
      lane({ id: 'feat', label: 'feature', isMain: true }),
      lane({ id: 'wt', label: 'a-worktree', isMain: false }),
      lane({ id: 'main', label: 'main', isMain: true })
    ]

    expect(sortWorktreeGroups(groups).map(g => g.label)).toEqual(['main', 'feature', 'a-worktree', 'kanban'])
  })
})

describe('mergeRepoWorktreeGroups (visual enhancer)', () => {
  it('injects a linked worktree lane discovered by git that has no sessions yet', () => {
    const repo = { id: '/repo', path: '/repo', groups: [lane({ id: '/repo::branch::main', label: 'main', isMain: true, path: '/repo' })] }
    const discovered: HermesGitWorktree[] = [
      { branch: 'feature', detached: false, isMain: false, locked: false, path: '/repo-wt-feature' }
    ]

    const merged = mergeRepoWorktreeGroups(repo, discovered)

    expect(merged.map(g => g.label)).toEqual(['main', 'feature'])
    // The injected lane is empty (visual only — never carries sessions).
    expect(merged.find(g => g.label === 'feature')?.sessions).toEqual([])
  })

  it('never spawns a lane per kanban task worktree', () => {
    const repo = { id: '/repo', path: '/repo', groups: [lane({ id: '/repo::branch::main', label: 'main', isMain: true, path: '/repo' })] }
    const discovered: HermesGitWorktree[] = [
      { branch: 'wt/t_aaaaaaaa', detached: false, isMain: false, locked: false, path: '/repo/.worktrees/t_aaaaaaaa' },
      { branch: 'wt/t_bbbbbbbb', detached: false, isMain: false, locked: false, path: '/repo/.worktrees/t_bbbbbbbb' }
    ]

    expect(mergeRepoWorktreeGroups(repo, discovered).map(g => g.label)).toEqual(['main'])
  })

  it('does not duplicate a lane already present from the backend (by id/path)', () => {
    const repo = {
      id: '/repo',
      path: '/repo',
      groups: [
        lane({ id: '/repo::branch::main', label: 'main', isMain: true, path: '/repo', sessions: [makeSession('/repo')] })
      ]
    }
    const discovered: HermesGitWorktree[] = [
      { branch: 'main', detached: false, isMain: true, locked: false, path: '/repo' }
    ]

    const merged = mergeRepoWorktreeGroups(repo, discovered)

    expect(merged).toHaveLength(1)
    // The backend lane keeps its session rows; the enhancer left it untouched.
    expect(merged[0].sessions).toHaveLength(1)
  })

  it('is a no-op when git worktree list is unavailable (remote backend)', () => {
    const groups = [lane({ id: '/repo::branch::main', label: 'main', isMain: true, path: '/repo' })]

    expect(mergeRepoWorktreeGroups({ id: '/repo', path: '/repo', groups }, undefined).map(g => g.label)).toEqual(['main'])
  })

  it('does not add a second "main" for a linked worktree checked out on main', () => {
    const groups = [lane({ id: '/repo::branch::main', label: 'main', isMain: true, path: '/repo', sessions: [makeSession('/repo')] })]
    const discovered: HermesGitWorktree[] = [
      { branch: 'main', detached: false, isMain: false, locked: false, path: '/repo/.worktrees/main-mirror' }
    ]

    expect(mergeRepoWorktreeGroups({ id: '/repo', path: '/repo', groups }, discovered).filter(g => g.label === 'main')).toHaveLength(1)
  })

  it('surfaces a user-named "New worktree" under .worktrees/ as its own lane', () => {
    const discovered: HermesGitWorktree[] = [
      { branch: 'hermes/test-gui-stuff', detached: false, isMain: false, locked: false, path: '/repo/.worktrees/test-gui-stuff' }
    ]

    const merged = mergeRepoWorktreeGroups({ id: '/repo', path: '/repo', groups: [] }, discovered)

    expect(merged.map(g => g.label)).toContain('hermes/test-gui-stuff')
  })
})

const makeProject = (id: string, folders: string[]): ProjectInfo => ({
  archived: false,
  board_slug: null,
  color: null,
  created_at: 0,
  description: null,
  folders: folders.map((path, i) => ({ added_at: 0, is_primary: i === 0, label: null, path })),
  icon: null,
  id,
  name: id,
  primary_path: folders[0] ?? null,
  slug: id
})

const projectNode = (over: Partial<SidebarProjectTree> & Pick<SidebarProjectTree, 'id'>): SidebarProjectTree => ({
  label: over.id,
  path: over.id,
  repos: [],
  sessionCount: 0,
  ...over
})

describe('placeLiveSession', () => {
  it('places a brand-new (unpersisted) session into its auto project + main lane', () => {
    // New session: cwd set to the repo, no git_repo_root/git_branch yet.
    const placement = placeLiveSession(makeSession('/www/app'), [])

    // Empty branch canonicalizes to the one "main" lane (no separate ::branch:: bucket).
    expect(placement).toMatchObject({ projectId: '/www/app', repoRoot: '/www/app', laneId: '/www/app::branch::main', laneLabel: 'main' })
  })

  it('routes a session under an explicit project folder to that project', () => {
    const placement = placeLiveSession(makeSession('/www/app/src', { git_repo_root: '/www/app', git_branch: 'feat' }), [
      makeProject('p_app', ['/www/app'])
    ])

    expect(placement).toMatchObject({ projectId: 'p_app', laneId: '/www/app::branch::feat', laneLabel: 'feat' })
  })

  it('skips cwd-less, kanban, and linked-worktree sessions (backend folds those)', () => {
    expect(placeLiveSession(makeSession(null), [])).toBeNull()
    expect(placeLiveSession(makeSession('/repo/.worktrees/t_aaaaaaaa'), [])).toBeNull()
    expect(placeLiveSession(makeSession('/elsewhere/wt', { git_repo_root: '/repo' }), [])).toBeNull()
  })
})

describe('overlayLiveLanes', () => {
  it('injects a live session into the matching main lane instantly', () => {
    const project = projectNode({
      id: '/www/app',
      isAuto: true,
      repos: [{ id: '/www/app', label: 'app', path: '/www/app', sessionCount: 0, groups: [] }]
    })
    const live = [makeSession('/www/app', { id: 'fresh', git_branch: 'main' })]

    const overlaid = overlayLiveLanes(project, live, [])
    const lane = overlaid.repos[0].groups.find(g => g.label === 'main')

    expect(lane?.sessions.map(session => session.id)).toContain('fresh')
    expect(overlaid.sessionCount).toBe(1)
  })

  it('does not duplicate a session already present in a backend lane', () => {
    const existing = makeSession('/www/app', { id: 'dup', git_branch: 'main' })
    const project = projectNode({
      id: '/www/app',
      repos: [
        {
          id: '/www/app',
          label: 'app',
          path: '/www/app',
          sessionCount: 1,
          groups: [lane({ id: '/www/app::branch::main', label: 'main', isMain: true, path: '/www/app', sessions: [existing] })]
        }
      ]
    })

    const overlaid = overlayLiveLanes(project, [existing], [])

    expect(overlaid.repos[0].groups.flatMap(g => g.sessions.map(s => s.id))).toEqual(['dup'])
  })
})

describe('overlayLivePreviews', () => {
  it('merges live sessions into a project preview, live first, capped to the limit', () => {
    const project = projectNode({
      id: '/www/app',
      previewSessions: [makeSession('/www/app', { id: 'old', started_at: 1, last_active: 1 })]
    })
    const live = [makeSession('/www/app', { id: 'fresh', started_at: 99, last_active: 99 })]

    const previews = overlayLivePreviews([project], live, [], 3)

    expect(previews['/www/app'].map(s => s.id)).toEqual(['fresh', 'old'])
  })
})
