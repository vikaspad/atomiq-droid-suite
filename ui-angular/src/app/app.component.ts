import { Component, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Subscription } from 'rxjs';
import { ApiService } from './api.service';

/** Root standalone component that collects user input (GitHub URL, generation mode, prompt, LLM config, optional file/API key), 
 * then orchestrates a long-running backend build job via ApiService */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule],
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.css']
})
export class AppComponent implements OnDestroy {
  title = 'Atomiq TestDroid — Powered by CrewAI';
  lastProgress = -1;

  /**
  * Reactive form capturing user inputs.
  * - githubUrl: required
  * - generation: 'unit' or 'bdd' (required)
  * - prompt: optional guidance for the generator
  * - provider/model: LLM selection with defaults
  * - apiKey: optional; passed through to backend if needed
  */
  form = this.fb.group({
    githubUrl: ['', Validators.required],
    generation: ['unit', Validators.required],
    prompt: [''],
    provider: ['openai'],
    model: ['gpt-4o-mini'],
    apiKey: ['']
  });

  //Optional file attached by the user
  file: File | null = null;
  //status line shown above the log
  status = 'Ready.';
  log = '';
  /** When the backend artifact is ready, this holds the direct download href. */
  downloadHref = '';
  /** Subscription to the progress stream so we can cancel/replace/cleanup. */
  sub?: Subscription;

  constructor(private fb: FormBuilder, private api: ApiService) { }

  /**
   * Handle <input type="file"> changes and store the first selected file.
   */
  onFile(e: Event) {
    const input = e.target as HTMLInputElement;
    this.file = input?.files?.[0] ?? null;
  }

  /**
   * Validate the form, create a backend job, and stream progress until completion.
   * Workflow:
   * 1) Guard: if form invalid -> set status and return.
   * 2) Reset UI state (status/log/downloadHref).
   * 3) Map 'generation' radio to boolean flags for unit vs BDD generation.
   * 4) POST build request -> jobId
   * 5) Subscribe to merged SSE + polling progress until succeeded/failed.
   * 6) Append unique progress lines; set artifact URL when available.
   * 7) On complete, fetch final job snapshot to finalize status and artifact href.
   */
  submit() {
    if (this.form.invalid) {
      this.status = 'Enter a GitHub URL';
      return;
    }
    this.status = 'Submitting...';
    this.log = '';
    this.downloadHref = '';

    const v = this.form.value;
    const generateUnitTests = v.generation === 'unit';
    const createBddFramework = v.generation === 'bdd';

    console.log('GEN CHOICE:', v.generation, {
      generateUnitTests, createBddFramework
    });

    //Calls api.build(...) to create a job; receives jobId.
    this.api.build({
      githubUrl: v.githubUrl || '',
      generateUnitTests,
      createBddFramework,
      prompt: v.prompt || '',
      llmProvider: v.provider || 'openai',
      llmModel: v.model || 'gpt-4o-mini',
      apiKey: v.apiKey || '',
      file: this.file
    }).subscribe({
      next: ({ jobId }) => {
        this.status = `Job accepted: ${jobId} — streaming...`;
        this.sub?.unsubscribe();
        this.lastProgress = -1;             // reset

        this.sub = this.api.progressUntilDone(jobId).subscribe({
          next: (d: any) => {
            const p = typeof d?.progress === 'number' ? d.progress : undefined;
            // Append progress lines only when the numeric % changes.
            if (p !== undefined && p !== this.lastProgress) {
              this.lastProgress = p;
              this.log += `[${p}%] ${d.status}: ${d.message}\n`;
            }
            // If backend already exposes artifactUrl, show it immediately
            if (d?.artifactUrl) this.downloadHref = this.api.artifactUrl(jobId);
          },
          complete: () => {
            // Final snapshot ensures we show the terminal status and the artifact link.
            this.api.getJob(jobId).subscribe((final: any) => {
              if (final?.status === 'succeeded') {
                this.status = 'Done.';
                this.downloadHref = this.api.artifactUrl(jobId); // <— always set
              } else {
                this.status = `Finished with status: ${final?.status || 'unknown'}`;
              }
            });
          }
        });
      },
      // Show a concise error message from the backend if present.
      error: (err) => this.status = `Error: ${err?.error?.error || err.message}`
    });
  }

  ngOnDestroy() { this.sub?.unsubscribe(); }
}
