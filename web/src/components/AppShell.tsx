import { NavLink, useParams } from "react-router-dom"
import type { ReactNode } from "react"

import { cn } from "@/lib/utils"
import { useEntity } from "@/lib/queries"

const TABS = [
  { to: "month", label: "Review" },
  { to: "review", label: "Inbox" },
  { to: "reports", label: "Reports" },
  { to: "accounts", label: "Accounts" },
  { to: "trial-balance", label: "Trial balance" },
  { to: "close", label: "Closing" },
]

function Tab({ to, label, entity }: { to: string; label: string; entity: string }) {
  return (
    <NavLink
      to={`/e/${entity}/${to}`}
      className={({ isActive }) =>
        cn(
          "relative whitespace-nowrap border-b-2 px-1 py-3 text-sm transition-colors",
          isActive
            ? "border-primary font-medium text-foreground"
            : "border-transparent text-muted-foreground hover:text-foreground",
        )
      }
    >
      {label}
    </NavLink>
  )
}

/** The frame around every entity page: the entity's name and the section tabs. */
export function AppShell({ children }: { children: ReactNode }) {
  const { entity = "" } = useParams()
  const { data } = useEntity(entity)

  return (
    <div className="min-h-dvh bg-background">
      <header className="border-b bg-card/60 backdrop-blur-sm">
        <div className="mx-auto max-w-6xl px-5">
          <div className="flex items-baseline gap-3 pt-5">
            <NavLink to="/" className="text-sm font-semibold tracking-tight text-muted-foreground hover:text-foreground">
              Haddon
            </NavLink>
            <span className="text-muted-foreground/40">/</span>
            <h1 className="truncate text-base font-semibold tracking-tight">
              {data?.legal_name ?? entity}
            </h1>
            {data ? (
              <span className="hidden text-xs text-muted-foreground sm:inline">
                {data.legal_form} · {data.country} · {data.base_currency}
              </span>
            ) : null}
          </div>
          {/* The -1px goes on the nav, not each tab, so the active underline sits on the header's border without
              a tab overflowing its row by 1px.  Tabs wrap rather than scroll, so neither axis can grow a
              scrollbar and every section stays reachable. */}
          <nav className="-mb-px mt-2 flex flex-wrap gap-x-6" aria-label="Sections">
            {TABS.map((t) => (
              <Tab key={t.to} {...t} entity={entity} />
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-5 py-7">{children}</main>
    </div>
  )
}
