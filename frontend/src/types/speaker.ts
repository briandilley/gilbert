/** Speaker subsystem types for the Settings UI and browser-echo hook. */

/** Response shape of the ``speaker.info`` WebSocket RPC. */
export interface SpeakerSystemInfo {
  enabled: boolean;
  /** Name of the primary (first-priority) speaker backend, e.g. ``"sonos"`` or ``"browser"``. */
  primary_backend: string;
  /** Names of all currently-running backends, sorted alphabetically. */
  active_backends: string[];
  /** Backends that failed to start during the last initialisation attempt. */
  startup_failures: { name: string; error: string }[];
}
