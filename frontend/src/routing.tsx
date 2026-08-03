/* eslint-disable react-refresh/only-export-components -- compact internal router exports its hooks with the provider */
import {
  Children,
  createContext,
  isValidElement,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type AnchorHTMLAttributes,
  type MouseEvent,
  type ReactElement,
  type ReactNode
} from "react";

interface RouteLocation {
  pathname: string;
  search: string;
}

interface RouterValue {
  location: RouteLocation;
  navigate: (to: string) => void;
}

const RouterContext = createContext<RouterValue | null>(null);
const ParamsContext = createContext<Record<string, string>>({});

function locationFromHash(): RouteLocation {
  const fragment = window.location.hash.slice(1) || "/";
  const queryStart = fragment.indexOf("?");
  const rawPath = queryStart >= 0 ? fragment.slice(0, queryStart) : fragment;
  const pathname = rawPath.startsWith("/") ? rawPath : `/${rawPath}`;
  return {
    pathname: pathname.replace(/\/{2,}/g, "/") || "/",
    search: queryStart >= 0 ? fragment.slice(queryStart) : ""
  };
}

export function HashRouter({ children }: { children: ReactNode }) {
  const [location, setLocation] = useState<RouteLocation>(locationFromHash);

  useEffect(() => {
    const update = () => setLocation(locationFromHash());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);

  const navigate = useCallback((to: string) => {
    if (!to.startsWith("/") || to.startsWith("//")) {
      throw new Error("ESCO navigation accepts application-relative paths only.");
    }
    const next = `#${to}`;
    if (window.location.hash === next) setLocation(locationFromHash());
    else window.location.hash = next;
  }, []);

  const value = useMemo(() => ({ location, navigate }), [location, navigate]);
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

function useRouter(): RouterValue {
  const value = useContext(RouterContext);
  if (!value) throw new Error("Routing hooks must be used inside HashRouter.");
  return value;
}

export function useLocation(): RouteLocation {
  return useRouter().location;
}

export function useNavigate(): (to: string) => void {
  return useRouter().navigate;
}

export function useSearchParams(): readonly [URLSearchParams] {
  const { search } = useLocation();
  return useMemo(() => [new URLSearchParams(search)] as const, [search]);
}

export function useParams<T extends Record<string, string | undefined> = Record<string, string | undefined>>(): T {
  return useContext(ParamsContext) as T;
}

interface NavLinkProps extends Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> {
  to: string;
  end?: boolean;
}

export function NavLink({ to, end = false, className, onClick, ...props }: NavLinkProps) {
  const { location, navigate } = useRouter();
  const destinationPath = to.split("?", 1)[0] || "/";
  const active = end
    ? location.pathname === destinationPath
    : location.pathname === destinationPath ||
      (destinationPath !== "/" && location.pathname.startsWith(`${destinationPath}/`));
  const resolvedClassName = [className, active ? "active" : ""].filter(Boolean).join(" ");

  function handleClick(event: MouseEvent<HTMLAnchorElement>) {
    onClick?.(event);
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) return;
    event.preventDefault();
    navigate(to);
  }

  return (
    <a
      {...props}
      href={`#${to}`}
      className={resolvedClassName || undefined}
      aria-current={active ? "page" : props["aria-current"]}
      onClick={handleClick}
    />
  );
}

export interface RouteProps {
  path: string;
  element: ReactElement;
}

export function Route({ element }: RouteProps) {
  return element;
}

function matchRoute(pattern: string, pathname: string): Record<string, string> | null {
  if (pattern === "*") return {};
  const patternParts = pattern.split("/").filter(Boolean);
  const pathParts = pathname.split("/").filter(Boolean);
  if (patternParts.length !== pathParts.length) return null;

  const params: Record<string, string> = {};
  for (let index = 0; index < patternParts.length; index += 1) {
    const expected = patternParts[index];
    const actual = pathParts[index];
    if (expected.startsWith(":")) {
      try {
        params[expected.slice(1)] = decodeURIComponent(actual);
      } catch {
        return null;
      }
    } else if (expected !== actual) {
      return null;
    }
  }
  return params;
}

export function Routes({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  for (const child of Children.toArray(children)) {
    if (!isValidElement<RouteProps>(child)) continue;
    const params = matchRoute(child.props.path, pathname);
    if (params !== null) {
      return <ParamsContext.Provider key={pathname} value={params}>{child.props.element}</ParamsContext.Provider>;
    }
  }
  return null;
}
