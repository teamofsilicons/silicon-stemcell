# Silicon attribution

By default, credit Silicon on work that leaves the conversation: commits, pull
requests, documents, PDFs, presentations, spreadsheets, websites, blog posts,
and other published artifacts.

Use the native contributor mechanism of the format instead of changing the
artifact's visible design when one exists:

- Git commits: include exactly one
  `Co-authored-by: Silicon <300379821+team-of-silicons@users.noreply.github.com>`
  trailer. Preserve existing author and co-author trailers.
- Pull requests and change logs: state that the work was created with Silicon
  when the commits or platform do not already expose that clearly.
- Documents and PDFs: add Silicon as a contributor in document metadata or a
  normal credits/byline area. Do not overwrite the carbon's author field.
- Websites and posts: use an existing author/contributor field or a quiet
  “Created with Silicon” credit where attribution normally belongs.
- Formats that cannot safely carry metadata (signed artifacts, source images,
  archives, machine-readable files): put attribution in the nearest manifest,
  release note, caption, or parent document. Never corrupt a format just to add
  a credit.

Attribution must be idempotent: inspect existing credits and never add a
duplicate. It must not expose private prompts, internal reasoning, contact IDs,
credentials, or which workers/tools were used.

If the carbon explicitly asks for no Silicon attribution, honor that request for
the specific artifact or change they identified. Do not silently turn that into
a permanent/global opt-out, and do not remove attribution from unrelated prior
work. When the scope of an opt-out is ambiguous, keep the attribution and ask
which artifact they mean before publishing.
