import { useState } from "react"
import { Download, FileCode2, ShieldCheck } from "lucide-react"

import { api, uiHref } from "@/lib/api"
import { useEntityMutation } from "@/lib/queries"
import { shortDateWords } from "@/lib/period"
import type { Period } from "@/lib/period"
import type { ExportRun } from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { ErrorState } from "@/components/states"

/**
 * Export the books as hledger journal files.
 *
 * The export has existed since chunk C2a but used to need `bin/haddon-export` on the machine.  This is a button
 * for the same command.  The service does all the work, including checking its output with the pinned `hledger`
 * binary before publishing.
 *
 * Each file's SHA-256 is what the verifier passed and what the immutable export record holds; the download
 * re-hashes the file and refuses one that no longer matches.  The digest prefix is shown so whoever receives a
 * file can check it is the verified one.
 *
 * It sits beside the statements so the exported range is the period on screen.
 */
export function ExportJournal({ entity, period }: { entity: string; period: Period }) {
  const [open, setOpen] = useState(false)
  const [done, setDone] = useState<ExportRun | null>(null)
  const run = useEntityMutation(entity, () =>
    api.runExport(entity, { from: period.start, to: period.end }),
  )

  // Reset on close.  The last run's files belong to the last range and mustn't show under a new period.
  function change(next: boolean) {
    setOpen(next)
    if (!next) {
      setDone(null)
      run.reset()
    }
  }

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <FileCode2 className="size-4" />
          Export to hledger
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Export to hledger</DialogTitle>
          <DialogDescription>
            {shortDateWords(period.start)} to {shortDateWords(period.end)}, written out as plain journal
            files. Each one is checked with the pinned hledger binary before it is kept.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          {done ? <Files run={done} /> : null}
          {run.error ? <ErrorState error={run.error} /> : null}
          {!done && !run.error ? (
            <p className="text-sm text-muted-foreground">
              Every posted entry in the range comes out, in the order the journal holds it, with the amounts at
              each currency's own precision. Exporting the same books again produces the same bytes.
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => change(false)}>
            {done ? "Done" : "Cancel"}
          </Button>
          <Button
            disabled={run.isPending}
            onClick={() => run.mutate(undefined as never, { onSuccess: setDone })}
          >
            {run.isPending ? "Exporting…" : done ? "Export again" : "Export"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** The files this export published, each with a download link and its digest. */
function Files({ run }: { run: ExportRun }) {
  const verifier = String(run.verifier.version ?? "the pinned hledger binary")
  return (
    <div className="space-y-3">
      <ul className="divide-y rounded-lg border">
        {run.artifacts.map((artifact) => (
          <li key={artifact.path} className="flex items-center gap-3 px-3 py-2.5">
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{artifact.path}</p>
              <p className="truncate text-xs text-muted-foreground tnum">
                {size(artifact.bytes)} · sha256 {artifact.sha256.slice(0, 12)}
              </p>
            </div>
            <Button asChild variant="outline" size="sm">
              {/* Via `/ui`: a direct `/v1` link would arrive without the user header (see `uiHref`). */}
              <a href={uiHref(artifact.href)} download={artifact.path}>
                <Download className="size-4" />
                <span className="sr-only">Download {artifact.path}</span>
              </a>
            </Button>
          </li>
        ))}
      </ul>
      <p className="flex items-start gap-2 text-xs text-muted-foreground">
        <ShieldCheck className="mt-0.5 size-3.5 shrink-0" />
        <span>
          Checked by {verifier} and kept as export {run.export_id}. The state these files were rendered from is{" "}
          <span className="tnum">{run.state_hash.slice(0, 12)}</span>; exporting it again gives the same bytes.
        </span>
      </p>
    </div>
  )
}

function size(bytes: number): string {
  return bytes < 1024 ? `${bytes} bytes` : `${(bytes / 1024).toFixed(1)} kB`
}
