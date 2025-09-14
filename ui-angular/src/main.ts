import { bootstrapApplication } from '@angular/platform-browser';
import { provideHttpClient, withFetch } from '@angular/common/http';
import { provideAnimations } from '@angular/platform-browser/animations';
import { AppComponent } from './app/app.component';

//browser entry point
bootstrapApplication(AppComponent, {
  providers: [
    // HttpClient for services (modern provider API; works in standalone apps)
    provideHttpClient(withFetch()),
    // Not strictly required, but avoids warnings and future animation use
    provideAnimations()
  ]
}).catch(err => console.error('Bootstrap error:', err));
