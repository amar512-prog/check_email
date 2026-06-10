/// <reference types="vite/client" />

interface GoogleCredentialResponse {
  credential: string;
}

interface GoogleIdentityServices {
  accounts: {
    id: {
      initialize(options: {
        client_id: string;
        callback(response: GoogleCredentialResponse): void;
        auto_select?: boolean;
        cancel_on_tap_outside?: boolean;
      }): void;
      renderButton(
        element: HTMLElement,
        options: Record<string, string | number | boolean>,
      ): void;
    };
  };
}

interface Window {
  google?: GoogleIdentityServices;
}

