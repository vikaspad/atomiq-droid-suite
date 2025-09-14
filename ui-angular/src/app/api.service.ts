import { Injectable, NgZone } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, Subject, interval, merge, of } from 'rxjs';
import { catchError, map, startWith, switchMap, takeWhile } from 'rxjs/operators';
import { environment } from '../environments/environment';

//Marks ApiService as a singleton service available app-wide.
@Injectable({ providedIn: 'root' })
export class ApiService {

  //allow switching API hosts per environment (dev/stage/prod) without code changes
  private base = environment.API_BASE || '';

  //HttpClient does REST calls; NgZone lets us handle SSE outside Angular’s change detection for performance, then re-enter safely
  constructor(private http: HttpClient, private zone: NgZone) { }

  //Assembles a FormData payload (GitHub URL, prompt, LLM provider/model, API key, toggles, optional file).
  //Sends POST /api/build and returns { jobId }.
  build(input: {
    githubUrl: string;
    generateUnitTests: boolean;
    createBddFramework: boolean;   // from the radio mapping
    prompt?: string;
    llmProvider?: string;
    llmModel?: string;
    apiKey?: string;
    file?: File | null;
  }) {
    const fd = new FormData();
    fd.append('github_url', input.githubUrl || '');
    fd.append('prompt', input.prompt || '');
    fd.append('llm_provider', input.llmProvider || 'openai');
    fd.append('llm_model', input.llmModel || 'gpt-4o-mini');
    if (input.apiKey) fd.append('api_key', input.apiKey);

    fd.append('generate_unit', String(!!input.generateUnitTests));
    fd.append('generate_bdd', String(!!input.createBddFramework));

    if (input.file) fd.append('file', input.file, input.file.name);
    return this.http.post<{ jobId: string }>('/api/build', fd);
  }

  //Opens an SSE connection to /api/jobs/:jobId/stream
  stream(jobId: string): Observable<{ progress: number; status: string; message: string }> {
    const url = `${this.base}/api/jobs/${jobId}/stream`;
    //real-time progress updates without polling.
    const subject = new Subject<{ progress: number; status: string; message: string }>();

    //Uses runOutsideAngular to avoid unnecessary change detection, then zone.run when emitting to consumers.
    this.zone.runOutsideAngular(() => {
      const es = new EventSource(url);
      es.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data);
          this.zone.run(() => subject.next(data));
        } catch { }
      };
      es.onerror = () => {
        es.close();
        this.zone.run(() => subject.complete());
      };
    });

    return subject.asObservable();
  }

  //fetch the latest job snapshot (status, progress, etc.)—used for polling or final state.
  getJob(jobId: string) {
    return this.http.get(`${this.base}/api/jobs/${jobId}`);
  }

  //direct download link for the generated ZIP/artifact.
  artifactUrl(jobId: string) {
    return `${this.base}/api/jobs/${jobId}/artifact`;
  }

  //resilient progress observable—keeps UI updated via SSE when available, falls back to polling on errors.
  progressUntilDone(jobId: string) {
    const poll$ = interval(1200).pipe(
      startWith(0),
      switchMap(() => this.getJob(jobId).pipe(catchError(() => of(null))))
    );
    const sse$ = this.stream(jobId).pipe(catchError(() => of(null)));
    return merge(sse$, poll$).pipe(
      map((d: any) => d || {}),
      takeWhile((d: any) => (d.status !== 'succeeded' && d.status !== 'failed'), true)
    );
  }
}
