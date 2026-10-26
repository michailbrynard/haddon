import { useEffect, useMemo, useRef, useState } from "react"
import { Check, ChevronsUpDown, Plus, Search } from "lucide-react"
import { toast } from "sonner"

import { api } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Command, CommandEmpty, CommandGroup, CommandItem, CommandList } from "@/components/ui/command"
import { Command as CommandPrimitive } from "cmdk"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ErrorState } from "@/components/states"
import { cn } from "@/lib/utils"
import { useAccounts, useEntityMutation } from "@/lib/queries"
import type { Account } from "@/lib/types"

const TYPE_ORDER = ["expense", "income", "liability", "asset", "equity"]

/** The five account types, each with a short hint.
 *
 * The type can't be read off the path (a chart can put a contra-asset under `assets:…`), so the form asks.
 * The hints matter because "asset or expense?" is a real decision for a payment on something that lasts.
 */
const TYPES: { value: string; label: string; hint: string }[] = [
  { value: "expense", label: "Expense", hint: "money spent" },
  { value: "income", label: "Income", hint: "money earned" },
  { value: "asset", label: "Asset", hint: "owned by the company, or owed to it" },
  { value: "liability", label: "Liability", hint: "owed by the company" },
  { value: "equity", label: "Equity", hint: "put in, or left in, by the owners" },
]

const title = (type: string) => `${type[0].toUpperCase()}${type.slice(1)}`

/** Group the usable accounts by type, expenses and income first. */
function grouped(accounts: Account[], commodity: string, excludeAccountId?: string) {
  const usable = accounts.filter(
    (a) => !a.archived && a.commodities.includes(commodity) && a.id !== excludeAccountId,
  )
  const byType = new Map<string, Account[]>()
  for (const account of usable) {
    const list = byType.get(account.type) ?? []
    list.push(account)
    byType.set(account.type, list)
  }
  return [...byType.entries()]
    .sort((a, b) => {
      const ia = TYPE_ORDER.indexOf(a[0])
      const ib = TYPE_ORDER.indexOf(b[0])
      return (ia < 0 ? TYPE_ORDER.length : ia) - (ib < 0 ? TYPE_ORDER.length : ib)
    })
    .map(([type, list]) => ({ type, list: list.sort((a, b) => a.path.localeCompare(b.path)) }))
}

/** Pick the account a line belongs to, or add one if the chart has nothing suitable.
 *
 * Why a dialog and not a popover: as a popover, the list couldn't be scrolled on a phone.  The booking dialog
 * is modal, so Radix mounts `react-remove-scroll`, which cancels scroll gestures whose target is outside the
 * dialog's content element.  Radix portals popover content to `document.body`, outside it, so every
 * `touchmove` over the list was `defaultPrevented`.  The mouse wheel was blocked the same way on desktop, but
 * typing and arrow keys still worked so it went unnoticed.  As its own dialog layer, the list is inside the
 * active scroll region.
 *
 * On a phone it's a bottom sheet; on wider screens a centred command palette.
 *
 * Typing filters on the whole path, so "equip" finds `assets:fixed:equipment` and "example" finds
 * `assets:receivables:trade:example-inc`.  Accounts are shown by their path, verbatim; the chart has no
 * display names and the UI doesn't invent them.
 *
 * The line's own cash account is excluded.  The cash side of a booking is the statement line itself, so
 * choosing it would only be refused.
 */
export function AccountPicker({
  entity,
  commodity,
  excludeAccountId,
  value,
  onChange,
  id,
}: {
  entity: string
  commodity: string
  excludeAccountId?: string
  value: string | null
  onChange: (accountId: string) => void
  id?: string
}) {
  const [open, setOpen] = useState(false)
  const { data: accounts = [], isPending } = useAccounts(entity)
  const selected = accounts.find((a) => a.id === value)

  return (
    <>
      <Button
        id={id}
        type="button"
        variant="outline"
        role="combobox"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
        className="w-full justify-between font-normal"
      >
        {/* Styled like an input's ::placeholder (same colour token, italic) so "Choose an account" can't be
            mistaken for an account with that name. */}
        <span className={cn("truncate", !selected && "text-placeholder italic")}>
          {selected ? selected.path : isPending ? "Loading accounts…" : "Choose an account"}
        </span>
        <ChevronsUpDown className="ml-2 size-4 shrink-0 opacity-50" />
      </Button>

      <AccountPickerDialog
        entity={entity}
        commodity={commodity}
        excludeAccountId={excludeAccountId}
        accounts={accounts}
        value={value}
        open={open}
        onOpenChange={setOpen}
        onChange={onChange}
      />
    </>
  )
}

/** The picker dialog: a bottom sheet on a phone, a centred palette on wider screens.
 *
 * It switches between choosing an account and adding one within the same dialog rather than stacking a
 * second dialog, since adding an account ends with choosing it.
 */
function AccountPickerDialog({
  entity,
  commodity,
  excludeAccountId,
  accounts,
  value,
  open,
  onOpenChange,
  onChange,
}: {
  entity: string
  commodity: string
  excludeAccountId?: string
  accounts: Account[]
  value: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onChange: (accountId: string) => void
}) {
  const [search, setSearch] = useState("")
  const [writing, setWriting] = useState(false)
  const groups = useMemo(
    () => grouped(accounts, commodity, excludeAccountId),
    [accounts, commodity, excludeAccountId],
  )

  useEffect(() => {
    if (!open) return
    setSearch("")
    setWriting(false)
  }, [open])

  function choose(accountId: string) {
    onChange(accountId)
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* On a phone, a bottom sheet filling most of the height; from `sm` up, a centred palette.  `dvh`
          rather than `vh` because on iOS Safari `vh` is taller than the visible screen, which would push the
          footer under the browser chrome. */}
      <DialogContent
        showCloseButton={false}
        // On a phone, focusing the search box would raise the on-screen keyboard over half the sheet before
        // anyone asked to type.  Focus the sheet itself instead (so Escape and screen readers still work);
        // the keyboard appears when the search is tapped.  On wider screens keep the default and focus the
        // search box.
        onOpenAutoFocus={(event) => {
          if (window.matchMedia("(min-width: 640px)").matches) return
          event.preventDefault()
          ;(event.currentTarget as HTMLElement | null)?.focus()
        }}
        className={cn(
          "flex flex-col gap-0 overflow-hidden p-0",
          "inset-x-0 bottom-0 left-0 top-auto h-[88dvh] max-h-none w-full max-w-none translate-x-0",
          "translate-y-0 rounded-b-none",
          "sm:inset-x-auto sm:bottom-auto sm:left-[50%] sm:top-[50%] sm:h-auto sm:max-h-[32rem] sm:w-full",
          "sm:max-w-lg sm:translate-x-[-50%] sm:translate-y-[-50%] sm:rounded-lg",
        )}
      >
        {writing ? (
          <WriteAccount
            entity={entity}
            commodity={commodity}
            accounts={accounts}
            onCancel={() => setWriting(false)}
            onWritten={choose}
            onChooseExisting={choose}
          />
        ) : (
          <ChooseAccount
            groups={groups}
            value={value}
            search={search}
            onSearch={setSearch}
            onChoose={choose}
            onWrite={() => setWriting(true)}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function ChooseAccount({
  groups,
  value,
  search,
  onSearch,
  onChoose,
  onWrite,
}: {
  groups: { type: string; list: Account[] }[]
  value: string | null
  search: string
  onSearch: (text: string) => void
  onChoose: (accountId: string) => void
  onWrite: () => void
}) {
  return (
    <>
      <DialogHeader className="shrink-0 border-b px-4 py-3 text-left sm:text-left">
        <DialogTitle className="text-base">Choose an account</DialogTitle>
      </DialogHeader>

      {/* Case-insensitive substring match on the whole path, so a fragment of any segment finds the account. */}
      <Command
        className="flex h-auto min-h-0 flex-1 flex-col rounded-none bg-transparent text-foreground"
        filter={(item, text) => (item.toLowerCase().includes(text.toLowerCase()) ? 1 : 0)}
      >
        <div className="flex shrink-0 items-center gap-2 border-b px-4">
          <Search className="size-4 shrink-0 opacity-50" />
          <CommandPrimitive.Input
            value={search}
            onValueChange={onSearch}
            placeholder="Search the chart…"
            className="h-12 w-full bg-transparent text-base outline-hidden sm:h-11 sm:text-sm"
          />
        </div>

        {/* The scroll container, inside the dialog content on purpose (see `AccountPicker`).
            `overscroll-contain` stops a flick at the end of the list from scrolling the page behind. */}
        <CommandList className="max-h-none min-h-0 flex-1 overscroll-contain p-1">
          <CommandEmpty className="px-4 py-8 text-center text-sm text-muted-foreground">
            No account matches.
          </CommandEmpty>
          {groups.map(({ type, list }) => (
            <CommandGroup key={type} heading={title(type)}>
              {list.map((account) => (
                <CommandItem
                  key={account.id}
                  value={account.path}
                  onSelect={() => onChoose(account.id)}
                  className="min-h-11 cursor-pointer py-2.5 text-base sm:min-h-0 sm:py-1.5 sm:text-sm"
                >
                  <Check
                    className={cn("mr-2 size-4 shrink-0", account.id === value ? "opacity-100" : "opacity-0")}
                  />
                  <span className="truncate">{account.path}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          ))}
        </CommandList>
      </Command>

      <div className="shrink-0 border-t p-2">
        <Button type="button" variant="ghost" onClick={onWrite} className="w-full justify-start gap-2 py-5">
          <Plus className="size-4" />
          Add an account to the chart
        </Button>
      </div>
    </>
  )
}

/** Add a new account to the chart.
 *
 * Its own step with its own submit button, never a side effect of saving a booking.  Accounts are permanent:
 * every later report carries them, and a mistaken one can only be archived.  So the form asks for the two
 * things that can't be guessed (path and type) and writes the account on a separate press.  Once written, the
 * new account is chosen for the line.
 *
 * Not asked for, on purpose:
 *
 * - the currency: it's the line's own.  Adding further currencies to an account is set-up work, done
 *   elsewhere.
 * - `is_cash`: left false.  A cash account is one a bank statement is reconciled against, not something to
 *   create in passing while booking.
 * - a reason: optional for account creation.  The audit action records who added the account and when.
 */
function WriteAccount({
  entity,
  commodity,
  accounts,
  onCancel,
  onWritten,
  onChooseExisting,
}: {
  entity: string
  commodity: string
  accounts: Account[]
  onCancel: () => void
  onWritten: (accountId: string) => void
  onChooseExisting: (accountId: string) => void
}) {
  // Starts empty rather than seeded with the search text.  A search fragment like "equip" isn't a path, and
  // prefilled it would read as an answer and end up as an account called `equip`.
  const [path, setPath] = useState("")
  const [type, setType] = useState<string | null>(null)
  const pathInput = useRef<HTMLInputElement>(null)

  // As with the list, don't auto-focus on a phone: with the keyboard up, the bottom sheet hides its footer and
  // the submit button would be off screen.
  useEffect(() => {
    if (window.matchMedia("(min-width: 640px)").matches) pathInput.current?.focus()
  }, [])

  const trimmed = path.trim()
  // The service refuses a path already in the chart whatever its state, so say so before submitting.  Only
  // offer the existing account if it's usable here: not archived, and holding this line's currency.
  const clash = accounts.find((a) => a.path === trimmed)
  const clashUsable = Boolean(clash && !clash.archived && clash.commodities.includes(commodity))
  const ready = trimmed.length > 0 && Boolean(type) && !clash

  const write = useEntityMutation(entity, async () => {
    if (!type) throw new Error("Choose what kind of account this is.")
    return api.createAccount(entity, { path: trimmed, type, commodities: [commodity] })
  })

  function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!ready || write.isPending) return
    write.mutate(undefined, {
      onSuccess: (result) => {
        toast.success("Added to the chart", {
          description: `${result.account.path} is in the chart now.`,
        })
        onWritten(result.account.id)
      },
    })
  }

  return (
    <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col">
      <DialogHeader className="shrink-0 border-b px-4 py-3 text-left sm:text-left">
        <DialogTitle className="text-base">Add an account to the chart</DialogTitle>
      </DialogHeader>

      <div className="min-h-0 flex-1 space-y-5 overflow-y-auto overscroll-contain px-4 py-4">
        <div className="space-y-2">
          <Label htmlFor="new-account-path">What it is called</Label>
          <Input
            id="new-account-path"
            ref={pathInput}
            value={path}
            onChange={(e) => setPath(e.target.value)}
            autoComplete="off"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            className="font-mono text-base sm:text-sm"
          />
          <p className="text-xs text-muted-foreground">
            The path it will be known by, exactly as it will read in every report. Colons separate it into
            groups, the way the accounts in the list are written.
          </p>
          {clash ? (
            <div className="rounded-md border border-attention-border bg-attention-surface px-3 py-2 text-sm">
              <p>
                The chart already has this account
                {clash.archived
                  ? ", and it is archived."
                  : clash.commodities.includes(commodity)
                    ? "."
                    : `, and it does not hold ${commodity}.`}
              </p>
              {clashUsable ? (
                <button
                  type="button"
                  onClick={() => onChooseExisting(clash.id)}
                  className="mt-1 text-sm font-medium underline underline-offset-2"
                >
                  Use the one that is already there
                </button>
              ) : null}
            </div>
          ) : null}
        </div>

        <div className="space-y-2">
          <Label>What kind of account it is</Label>
          <div className="grid gap-1.5">
            {TYPES.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => setType(option.value)}
                aria-pressed={type === option.value}
                className={cn(
                  "flex min-h-11 items-baseline gap-2 rounded-md border px-3 py-2 text-left transition-colors",
                  type === option.value
                    ? "border-primary bg-primary/10"
                    : "border-border hover:bg-muted/50",
                )}
              >
                <span className="text-sm font-medium">{option.label}</span>
                <span className="text-xs text-muted-foreground">{option.hint}</span>
              </button>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">
            Expense and income land in the income statement for the year; the other three stay on the balance
            sheet. Which one this is cannot be read off the name, so nothing is guessed from it.
          </p>
        </div>

        <div className="space-y-1 rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          <p>It will hold {commodity}, the currency of the line being booked.</p>
          <p>
            An account stays in the chart once it is written; one that turns out to be wrong is archived, not
            removed.
          </p>
        </div>

        {write.error ? <ErrorState error={write.error} /> : null}
      </div>

      <div className="flex shrink-0 gap-2 border-t p-3">
        <Button type="button" variant="ghost" onClick={onCancel} className="flex-1">
          Back
        </Button>
        <Button type="submit" disabled={!ready || write.isPending} className="flex-1">
          {write.isPending ? "Adding…" : "Add it to the chart"}
        </Button>
      </div>
    </form>
  )
}
