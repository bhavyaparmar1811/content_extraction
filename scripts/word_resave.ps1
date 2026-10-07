# Open a .docx in Microsoft Word (hidden, read-only, no repair) and save a copy as Word writes it.
# Used by tests/test_word_roundtrip.py to check that normalized templates survive a Word save.
# Prints one line: "pages=<n> words=<n> content_controls=<n>". Exits 1 if Word can't open the file.
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Target
)

$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
$code = 0
try {
    $doc = $word.Documents.Open($Source, $false, $true, $false, "", "", $false, "", "", 0, 0, $false, $false)
    "pages={0} words={1} content_controls={2}" -f $doc.ComputeStatistics(2), $doc.ComputeStatistics(0), $doc.ContentControls.Count
    $doc.SaveAs2($Target, 16)
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
