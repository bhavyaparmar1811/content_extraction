# Open a .docx in Microsoft Word (hidden, read-only, no repair) and print the page each bookmark starts on.
# Used by the renderer to fill in the table of contents' page numbers (app/services/migration_v2/render/pages.py).
# Word only lays the document out; nothing is saved. Prints one "<bookmark>=<page>" line per bookmark found.
# Exits 1 if Word can't open the file.
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Names   # bookmark names, separated by "|"
)

$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
$code = 0
try {
    # The document needs a window (Word itself stays hidden): without one, page numbers read as -1.
    $doc = $word.Documents.Open($Source, $false, $true, $false, "", "", $false, "", "", 0, 0, $true, $false)
    $doc.ActiveWindow.View.Type = 3     # wdPrintView: pages as printed
    $doc.Bookmarks.ShowHidden = $true   # TOC bookmarks start with "_"
    $doc.Repaginate()
    foreach ($name in $Names.Split("|")) {
        if ($name -and $doc.Bookmarks.Exists($name)) {
            # 1 = wdActiveEndAdjustedPageNumber: the number PAGEREF shows (honours page-numbering restarts)
            "{0}={1}" -f $name, $doc.Bookmarks.Item($name).Range.Information(1)
        }
    }
    $doc.Close($false)
}
catch {
    "ERROR: " + $_.Exception.Message
    $code = 1
}
finally {
    $word.Quit()  # read-only and unmodified, so Word has nothing to prompt about
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($word)
}
exit $code
