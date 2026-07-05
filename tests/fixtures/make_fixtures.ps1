# Generates the two-channel fake-meeting fixture WAVs via Windows SAPI TTS.
# mic.wav   = the user ("Me", David voice)
# system.wav = two remote speakers (Zira + David) for diarization tests.
Add-Type -AssemblyName System.Speech
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer

function Speak-Lines($outFile, $lines) {
    $synth.SetOutputToWaveFile($outFile)
    foreach ($l in $lines) {
        $synth.SelectVoice($l[0])
        $synth.Rate = $l[1]
        $synth.Speak($l[2])
        # short silence between turns
        $b = New-Object System.Speech.Synthesis.PromptBuilder
        $b.AppendBreak([TimeSpan]::FromMilliseconds(700))
        $synth.Speak($b)
    }
    $synth.SetOutputToDefaultAudioDevice()
}

Speak-Lines (Join-Path $dir "mic.wav") @(
    @("Microsoft David Desktop", 0, "Thanks for joining. Today I want to review the recorder project timeline and the billing dashboard."),
    @("Microsoft David Desktop", 0, "I can take the action item to finish the transcription pipeline by Friday."),
    @("Microsoft David Desktop", 0, "Let us also decide on the database. I propose we keep everything as plain files for now.")
)

Speak-Lines (Join-Path $dir "system.wav") @(
    @("Microsoft Zira Desktop", 0, "Sounds good. On my side, the client asked about exporting summaries to email every week."),
    @("Microsoft David Desktop", 2, "I disagree about plain files. We should at least consider sequel light for the archive index."),
    @("Microsoft Zira Desktop", 0, "Okay, decision made. We start with plain files and revisit sequel light next month."),
    @("Microsoft David Desktop", 2, "Fine. My action item is to send the export requirements document by Tuesday.")
)

$synth.Dispose()
Get-ChildItem $dir -Filter *.wav | ForEach-Object { "{0} {1:N0} bytes" -f $_.Name, $_.Length }
