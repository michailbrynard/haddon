import { Download, ExternalLink, FileText, Eye } from "lucide-react"

import { api } from "@/lib/api"
import { useDocument } from "@/lib/queries"
import type { StatementDocument } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"

/** A human-readable byte count.  Not money, so float rounding is fine. */
function bytes(count: number): string {
  if (count < 1024) return `${count} bytes`
  const units = ["KB", "MB", "GB"]
  let value = count / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`
}

/**
 * The document behind a booking, with buttons to open or download it.
 *
 * Before issue 0007 this showed a SHA-256 and `blob_path`, neither of which a reviewer can use.  Now the page
 * opens the file, and the path is gone from the API.
 *
 * The URL comes from the service's `links.content`, not built from the document id.  The service omits the
 * link when it doesn't hold the bytes, so a document held elsewhere simply has no buttons.
 *
 * "Open" asks for `inline`, which the service honours only for media types that can't execute; "Download"
 * always works.
 *
 * The hash is still available behind a toggle, as the technical id (pitfall 3).
 */
export function Evidence({
  entity,
  documentId,
  document,
  caption,
  page,
  locator,
}: {
  entity: string
  documentId: string
  /** What the citing record already knows about the file, so the name and type render before the fetch. */
  document: StatementDocument
  caption?: string
  page?: number | null
  locator?: string | null
}) {
  const record = useDocument(entity, documentId)
  const content = api.documentContentHref(record.data?.document.links.content)
  const view = api.documentContentHref(record.data?.document.links.content, { inline: true })
  const external = document.storage === "external"

  return (
    <div className="rounded-lg border bg-card px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 gap-2.5">
          <FileText className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">{document.filename}</p>
            <p className="text-xs text-muted-foreground">
              {document.media_type} · {bytes(document.size_bytes)}
              {page ? ` · page ${page}` : ""}
            </p>
            {caption ? <p className="mt-1 text-sm">{caption}</p> : null}
            {locator ? <p className="text-xs text-muted-foreground">{locator}</p> : null}
          </div>
        </div>

        {external ? (
          <p className="text-xs text-muted-foreground">
            Held elsewhere
            {document.external_ref ? (
              <>
                {" — "}
                <span className="break-all font-mono">{document.external_ref}</span>
              </>
            ) : null}
          </p>
        ) : (
          <div className="flex shrink-0 gap-2">
            {/* `rel="noreferrer"` on both, so a document opened in a new tab isn't sent the service URL, which
                names the entity. */}
            <Button asChild size="sm" variant="outline" disabled={!view}>
              <a href={view ?? "#"} target="_blank" rel="noreferrer">
                <Eye className="size-3.5" />
                Open
              </a>
            </Button>
            <Button asChild size="sm" variant="ghost" disabled={!content}>
              <a href={content ?? "#"} download={document.filename} rel="noreferrer">
                <Download className="size-3.5" />
                Download
              </a>
            </Button>
          </div>
        )}
      </div>

      <Collapsible>
        <CollapsibleTrigger className="mt-2 text-xs text-muted-foreground hover:text-foreground">
          Fingerprint
        </CollapsibleTrigger>
        <CollapsibleContent>
          <p className="mt-1 break-all font-mono text-xs text-muted-foreground">{document.sha256}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            The SHA-256 the file was registered under. The service checks the bytes against it before serving
            them, so a document that has been replaced since it was filed will not open at all.
          </p>
        </CollapsibleContent>
      </Collapsible>
    </div>
  )
}

/** Compact version for lists: one line, one link, no fingerprint. */
export function EvidenceLine({
  entity,
  documentId,
  document,
}: {
  entity: string
  documentId: string
  document: StatementDocument
}) {
  const record = useDocument(entity, documentId)
  const content = api.documentContentHref(record.data?.document.links.content, { inline: true })
  return (
    <span className="inline-flex items-center gap-1.5">
      <FileText className="size-3.5 shrink-0 text-muted-foreground" />
      {content ? (
        <a className="underline underline-offset-4" href={content} target="_blank" rel="noreferrer">
          {document.filename}
        </a>
      ) : (
        <span>{document.filename}</span>
      )}
      {document.storage === "external" ? (
        <ExternalLink className="size-3 shrink-0 text-muted-foreground" aria-label="held elsewhere" />
      ) : null}
    </span>
  )
}
