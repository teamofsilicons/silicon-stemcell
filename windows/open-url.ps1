param([Parameter(Mandatory)][string]$Url)
$ErrorActionPreference = 'Stop'
$address = $null
if (![Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$address) -or $address.Scheme -notin @('http', 'https')) {
    throw 'Only HTTP and HTTPS browser addresses are supported.'
}
Start-Process -FilePath $address.AbsoluteUri
