export interface DashboardCard {
  title: string;
  description: string;
  url: string;
  icon: string;
  required_role: string;
}

export interface NavItem {
  label: string;
  description: string;
  /** URL for navigation items. Absent when the item is an action trigger. */
  url?: string;
  icon: string;
  required_role: string;
  /** Named RPC-triggering action. When set, clicking the item opens a
   *  confirm dialog and invokes the corresponding handler instead of
   *  navigating. The frontend decides how to present each known action. */
  action?: "restart_host";
}

export interface NavGroup {
  key: string;
  label: string;
  description: string;
  url: string;
  icon: string;
  items: NavItem[];
}

export interface DashboardResponse {
  cards: DashboardCard[];
  nav: NavGroup[];
  /** True when the server's speaker service has the ``browser`` backend
   *  loaded — gates the TopBar's BrowserSpeakerControl icon. */
  browser_speaker_available?: boolean;
}
